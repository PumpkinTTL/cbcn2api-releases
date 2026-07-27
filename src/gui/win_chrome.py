"""Win32 无边框窗口装饰：句柄查找、图标、可拉伸边框、去框架亮边、圆角。

为什么需要这个文件
------------------
pywebview 的 ``frameless=True`` 在 Windows 上等价于 WinForms ``FormBorderStyle.None``，
会同时去掉 ``WS_CAPTION`` 和 ``WS_THICKFRAME``。后者才是「可拉伸边框」的来源。

而 pywebview 里 ``resizable``（winforms.py:231）和 ``frameless``（:269）是先后两段
独立代码，后者无条件覆写 FormBorderStyle，所以 ``resizable=True`` 对 frameless 窗口
完全无效 —— 参数层面无解。后端里也没有任何 WM_NCHITTEST / WM_NCCALCSIZE 处理。
补回 ``WS_THICKFRAME`` 是不改 pywebview 源码的唯一办法。

三件事，按顺序调用
------------------
1. ``enable_resize_border()`` —— 加 ``WS_THICKFRAME``，拉伸回来。
2. ``suppress_nc_frame()``    —— 有了 THICKFRAME，Windows 会在客户区外留一圈非客户区；
   没有 ``WS_CAPTION`` 可画，DWM 就用系统框架色填它，表现为一条亮边（HTML 画不到
   那里）。拦 ``WM_NCCALCSIZE`` 把这圈厚度归零，DWM 便无处可画。
3. ``set_rounded_corners()``  —— 显式要求 Win11 圆角，不依赖系统默认行为。

已排除的两条弯路（别再试了）
----------------------------
- ``create_window(shadow=False)``：pywebview 在 shadow=True 时会调
  ``DwmExtendFrameIntoClientArea(1,1,1,1)`` 并把 NCRENDERING_POLICY 设为
  NCRP_ENABLED，看着像成因，但实测关掉它亮边照旧，只白丢投影和圆角。
- ``DWMWA_BORDER_COLOR = DWMWA_COLOR_NONE``：语义是「该窗口不画边框」，实测
  亮边仍在，还会连带去掉 Win11 圆角。

这套组合（THICKFRAME + NCCALCSIZE 清零 + 显式圆角）就是 Electron / Tauri
处理无边框窗口的标准配方。
"""

import ctypes
import os
import time
from ctypes import wintypes

__all__ = [
    "find_main_hwnd",
    "set_window_icon",
    "enable_resize_border",
    "suppress_nc_frame",
    "set_rounded_corners",
    "resize_delta",
]

GWL_STYLE = -16
GWLP_WNDPROC = -4
WS_THICKFRAME = 0x00040000
WM_SETICON = 0x0080
WM_NCCALCSIZE = 0x0083
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x00000010
# NOSIZE | NOMOVE | NOZORDER | FRAMECHANGED
_SWP_RESTYLE = 0x0001 | 0x0002 | 0x0004 | 0x0020
SM_CXSIZEFRAME = 32
SM_CYSIZEFRAME = 33
SM_CXPADDEDBORDER = 92

# DwmSetWindowAttribute：圆角偏好（Windows 11 build 22000+）
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_DEFAULT = 0
DWMWCP_DONOTROUND = 1
DWMWCP_ROUND = 2
DWMWCP_ROUNDSMALL = 3

# LRESULT / LONG_PTR 是指针宽度。用 c_ssize_t 让 32/64 位都对；
# ctypes 默认的 c_int 会在 x64 下截断句柄和返回值。
LRESULT = ctypes.c_ssize_t
_IS_64BIT = ctypes.sizeof(ctypes.c_void_p) == 8

user32 = ctypes.WinDLL("user32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)

WNDPROC = ctypes.WINFUNCTYPE(
    LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)


class NCCALCSIZE_PARAMS(ctypes.Structure):
    """rgrc[0] 进来是系统提议的窗口矩形，返回时应为期望的客户区矩形。"""

    _fields_ = [("rgrc", wintypes.RECT * 3), ("lppos", ctypes.c_void_p)]


def _sig(fn, restype, *argtypes):
    fn.restype = restype
    fn.argtypes = list(argtypes)
    return fn


_sig(user32.FindWindowExW, wintypes.HWND,
     wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR)
_sig(user32.GetWindowThreadProcessId, wintypes.DWORD,
     wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
_sig(user32.SetWindowPos, wintypes.BOOL, wintypes.HWND, wintypes.HWND,
     ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint)
_sig(user32.LoadImageW, wintypes.HANDLE, wintypes.HINSTANCE, wintypes.LPCWSTR,
     wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT)
_sig(user32.CallWindowProcW, LRESULT, ctypes.c_void_p,
     wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
_sig(user32.GetWindowRect, wintypes.BOOL, wintypes.HWND,
     ctypes.POINTER(wintypes.RECT))
_sig(user32.SetWindowPos, wintypes.BOOL, wintypes.HWND, wintypes.HWND,
     ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint)
_sig(user32.IsZoomed, wintypes.BOOL, wintypes.HWND)
_sig(user32.GetSystemMetrics, ctypes.c_int, ctypes.c_int)
_sig(dwmapi.DwmSetWindowAttribute, ctypes.c_long,
     wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD)

# 32 位 Windows 没有 GetWindowLongPtrW 这个导出符号（它是指向 GetWindowLongW 的宏）。
_get_window_long = user32.GetWindowLongPtrW if _IS_64BIT else user32.GetWindowLongW
_set_window_long = user32.SetWindowLongPtrW if _IS_64BIT else user32.SetWindowLongW
_sig(_get_window_long, LRESULT, wintypes.HWND, ctypes.c_int)
_sig(_set_window_long, LRESULT, wintypes.HWND, ctypes.c_int, LRESULT)


def find_main_hwnd(title, timeout=15.0, interval=0.15):
    """按标题查找**本进程**的顶层窗口，轮询直到出现或超时。

    比裸 FindWindowW 可靠两点：轮询而非 sleep 固定时长（慢机器、冷启动、
    打包后 WebView2 初始化慢都不会漏）；校验 PID，不会抓到别的进程里的同名窗口。
    """
    my_pid = os.getpid()
    deadline = time.monotonic() + timeout
    while True:
        hwnd = None
        while True:
            hwnd = user32.FindWindowExW(None, hwnd, None, title)
            if not hwnd:
                break
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == my_pid:
                return hwnd
        if time.monotonic() >= deadline:
            return 0
        time.sleep(interval)


def set_window_icon(hwnd, ico_path):
    """通过 WM_SETICON 设置窗口图标（大图标 + 16px 小图标）。"""
    if not hwnd or not ico_path or not os.path.exists(ico_path):
        return False
    big = user32.LoadImageW(None, ico_path, IMAGE_ICON, 0, 0, LR_LOADFROMFILE)
    small = user32.LoadImageW(None, ico_path, IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
    if big:
        user32.SendMessageW(hwnd, WM_SETICON, 1, big)
    if small:
        user32.SendMessageW(hwnd, WM_SETICON, 0, small)
    return bool(big or small)


def enable_resize_border(hwnd):
    """加回 WS_THICKFRAME，恢复原生拉伸命中测试。frameless 窗口必需。"""
    if not hwnd:
        return False
    style = _get_window_long(hwnd, GWL_STYLE)
    if not style:
        return False
    if style & WS_THICKFRAME:
        return True
    _set_window_long(hwnd, GWL_STYLE, style | WS_THICKFRAME)
    user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, _SWP_RESTYLE)
    return True


def set_rounded_corners(hwnd, round_corners=True):
    """显式设置 Win11 圆角偏好，不依赖系统默认推断。

    改过窗口样式和非客户区之后，系统对「该不该圆角」的判断可能变化，
    所以这里明确声明一次。Win10 不支持属性 33，返回 False，无副作用。
    """
    if not hwnd:
        return False
    value = wintypes.DWORD(DWMWCP_ROUND if round_corners else DWMWCP_DONOTROUND)
    hr = dwmapi.DwmSetWindowAttribute(
        hwnd, DWMWA_WINDOW_CORNER_PREFERENCE,
        ctypes.byref(value), ctypes.sizeof(value),
    )
    return hr == 0


# WNDPROC 是 ctypes 回调对象，必须保持模块级强引用 —— 一旦被 GC 回收，
# 窗口过程指针就成了野指针，下一条窗口消息直接把进程打崩。
_wndproc_refs = {}
_old_wndproc = {}


def _make_wndproc(key):
    def _proc(hwnd, msg, wparam, lparam):
        prev = _old_wndproc.get(key, 0)
        if msg == WM_NCCALCSIZE and wparam:
            try:
                # 返回 0 且不缩 rgrc[0]，客户区就等于整个窗口矩形：
                # 非客户区厚度归零，DWM 没有框架可画，亮边消失。
                p = ctypes.cast(lparam, ctypes.POINTER(NCCALCSIZE_PARAMS)).contents
                if user32.IsZoomed(hwnd):
                    # 最大化时系统故意把窗口矩形撑到「工作区 + 边框厚度」，
                    # 本来由那圈非客户区吸收。圈没了就得按同一指标缩回去，
                    # 否则客户区四边溢出屏幕、内容被裁。
                    pad = user32.GetSystemMetrics(SM_CXPADDEDBORDER)
                    cx = user32.GetSystemMetrics(SM_CXSIZEFRAME) + pad
                    cy = user32.GetSystemMetrics(SM_CYSIZEFRAME) + pad
                    p.rgrc[0].left += cx
                    p.rgrc[0].top += cy
                    p.rgrc[0].right -= cx
                    p.rgrc[0].bottom -= cy
            except Exception:
                # 异常绝不能穿回 Win32，退回默认处理。
                if prev:
                    return user32.CallWindowProcW(prev, hwnd, msg, wparam, lparam)
                return 0
            return 0
        if prev:
            return user32.CallWindowProcW(prev, hwnd, msg, wparam, lparam)
        return 0

    return WNDPROC(_proc)


def suppress_nc_frame(hwnd):
    """子类化窗口过程，拦 WM_NCCALCSIZE 去掉 DWM 画的框架亮边。

    resize 不在这里做：pywebview 的 WebView2 子控件铺满客户区，鼠标消息被它
    先吃掉，系统对顶层窗口的边缘命中测试（WS_THICKFRAME 默认行为）收不到；
    发 WM_NCLBUTTONDOWN(HT*) 让系统进 sizing loop 也起不来（光标变但拖不动）。
    所以彻底绕开 Win32 sizing —— 前端 JS 自己算鼠标 delta，每帧调 resize_delta()
    用 GetWindowRect + SetWindowPos 直接落尺寸。详见 resize_delta。
    """
    if not hwnd:
        return False
    key = int(hwnd)
    if key in _old_wndproc:  # 幂等，避免重复子类化套娃
        return True

    proc = _make_wndproc(key)
    prev = _set_window_long(hwnd, GWLP_WNDPROC,
                            ctypes.cast(proc, ctypes.c_void_p).value)
    if not prev:
        return False
    _wndproc_refs[key] = proc  # 防 GC
    _old_wndproc[key] = prev

    # 主动触发一次 WM_NCCALCSIZE，让新的客户区矩形立即生效。
    user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, _SWP_RESTYLE)
    return True


# 方向 -> 该边是否随鼠标移动。key 与前端 .rz 触发条的 data-dir 对齐。
# 每项 (move_left, move_top, move_right, move_bottom)：True 表示这条边跟着鼠标走。
_DIR_EDGES = {
    "left":        (True,  False, False, False),
    "right":       (False, False, True,  False),
    "top":         (False, True,  False, False),
    "bottom":      (False, False, False, True),
    "topleft":     (True,  True,  False, False),
    "topright":    (False, True,  True,  False),
    "bottomleft":  (True,  False, False, True),
    "bottomright": (False, False, True,  True),
}

# 最小尺寸兜底，低于这个值 SetWindowPos 拒绝执行（main.py 设的 min_size 是 900x600，
# 这里取略小一点，避免拖到极小后卡死；前端也会在 JS 端 clamp，这里是双保险）。
_MIN_W = 400
_MIN_H = 300


def resize_delta(hwnd, direction, dx, dy):
    """前端 JS 每帧调用，按鼠标增量直接改窗口矩形。

    不走 Win32 sizing modal loop（在 frameless + WebView2 下起不来），而是每帧
    GetWindowRect 拿当前矩形，按方向把 dx/dy 作用到对应边，SetWindowPos 落回去。
    等价于自己实现一遍系统 sizing，但完全在应用层，不依赖任何系统 sizing 状态。

    dx/dy 是这一帧的鼠标位移（逻辑像素），符号：鼠标右下移为正。
    direction 决定哪些边跟着鼠标走、以及反向边的尺寸如何变（拖左边时右边不动、
    宽度随 dx 减小，表现为左缘跟随鼠标）。
    """
    if not hwnd:
        return False
    edges = _DIR_EDGES.get(direction)
    if edges is None:
        return False
    if user32.IsZoomed(hwnd):
        # 最大化状态下不允许 resize，否则窗口矩形会脱离工作区。
        return False

    move_left, move_top, move_right, move_bottom = edges
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return False

    left, top, right, bottom = rect.left, rect.top, rect.right, rect.bottom

    # 拖左边/上边时，那条边跟随鼠标，对边不动 —— 尺寸变化方向与 dx/dy 相反。
    if move_left:
        left = min(left + dx, right - _MIN_W)
    if move_top:
        top = min(top + dy, bottom - _MIN_H)
    if move_right:
        right = max(right + dx, left + _MIN_W)
    if move_bottom:
        bottom = max(bottom + dy, top + _MIN_H)

    # SWP_NOZORDER=0x0004 | SWP_NOACTIVATE=0x0010：不动 Z 序、不抢焦点。
    user32.SetWindowPos(hwnd, None, left, top, right - left, bottom - top,
                        0x0004 | 0x0010)
    return True
