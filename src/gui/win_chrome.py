"""Win32 无边框窗口装饰：句柄查找、图标、圆角、resize。

为什么需要这个文件
------------------
pywebview 的 ``frameless=True`` 在 Windows 上等价于 WinForms ``FormBorderStyle.None``，
会同时去掉 ``WS_CAPTION`` 和 ``WS_THICKFRAME``。窗口因此没有原生边框，也没有非客户区，
DWM 无框架可画 —— frameless 窗口本身就是干净的无边框外观，不需要任何样式补丁。

resize 怎么做
-------------
系统原生 sizing（WS_THICKFRAME hit-test）和发 WM_NCLBUTTONDOWN 进 sizing loop 在
frameless + WebView2 下都不工作（WebView2 子控件铺满客户区，吃掉鼠标消息）。
所以彻底绕开 Win32 sizing：前端 JS 自己算鼠标 delta，每帧调 ``resize_delta()```，
用 GetWindowRect + SetWindowPos 直接落尺寸。不依赖 WS_THICKFRAME，不需要任何窗口
样式改动。

历史上走过的弯路（已废弃，别再走）
----------------------------------
曾经为了「恢复原生拉伸」给窗口补过 ``WS_THICKFRAME``，再用子类化拦 ``WM_NCCALCSIZE``
消除它带来的非客户区白边。这套「先加后删」的机制有两个问题：

1. THICKFRAME 带来的非客户区被 DWM 用框架色填充 → 白边；suppress_nc_frame 拦
   NCCALCSIZE 把它归零。但两步在后台线程顺序执行，中间帧 DWM 可能把白边画出来 →
   首次打开闪白边（重新 resize 或重开才消失）。
2. 改用前端 delta resize 后，WS_THICKFRAME 不再被任何功能依赖，整个机制成了纯负担。

所以现在全部删除。frameless 窗口保持原生 FormBorderStyle.None，干净无白边。

现在只做两件事
--------------
1. ``set_window_icon()``     —— 任务栏/标题栏图标。
2. ``set_rounded_corners()`` —— 显式声明 Win11 圆角（零风险保险，不依赖系统默认推断）。
"""

import ctypes
import os
import time
from ctypes import wintypes

__all__ = [
    "find_main_hwnd",
    "set_window_icon",
    "set_rounded_corners",
    "resize_delta",
]

WM_SETICON = 0x0080
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x00000010

# DwmSetWindowAttribute：圆角偏好（Windows 11 build 22000+）
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_DEFAULT = 0
DWMWCP_DONOTROUND = 1
DWMWCP_ROUND = 2
DWMWCP_ROUNDSMALL = 3

user32 = ctypes.WinDLL("user32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)


def _sig(fn, restype, *argtypes):
    fn.restype = restype
    fn.argtypes = list(argtypes)
    return fn


_sig(user32.FindWindowExW, wintypes.HWND,
     wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR)
_sig(user32.GetWindowThreadProcessId, wintypes.DWORD,
     wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
_sig(user32.LoadImageW, wintypes.HANDLE, wintypes.HINSTANCE, wintypes.LPCWSTR,
     wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT)
_sig(user32.SendMessageW, ctypes.c_ssize_t,
     wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
_sig(user32.GetWindowRect, wintypes.BOOL, wintypes.HWND,
     ctypes.POINTER(wintypes.RECT))
_sig(user32.SetWindowPos, wintypes.BOOL, wintypes.HWND, wintypes.HWND,
     ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint)
_sig(user32.IsZoomed, wintypes.BOOL, wintypes.HWND)
_sig(dwmapi.DwmSetWindowAttribute, ctypes.c_long,
     wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD)


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


def set_rounded_corners(hwnd, round_corners=True):
    """显式设置 Win11 圆角偏好，不依赖系统默认推断。

    Win10 不支持属性 33，返回 False，无副作用。
    """
    if not hwnd:
        return False
    value = wintypes.DWORD(DWMWCP_ROUND if round_corners else DWMWCP_DONOTROUND)
    hr = dwmapi.DwmSetWindowAttribute(
        hwnd, DWMWA_WINDOW_CORNER_PREFERENCE,
        ctypes.byref(value), ctypes.sizeof(value),
    )
    return hr == 0


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
