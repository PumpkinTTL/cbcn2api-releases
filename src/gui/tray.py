"""系统托盘：最小化到托盘 + 托盘图标交互（恢复窗口 / 退出）。

窗口最小化（pywebview minimized 事件）时隐藏主窗口并添加托盘图标，
任务栏不再占位，避免误点关闭。托盘左键/双击恢复窗口，右键菜单：
显示主界面 / 退出。

纯 Win32 实现（Shell_NotifyIcon + 主窗口 WndProc 子类化接收回调消息）。
所有 Win32 操作必须在主窗口所属线程（UI 线程）执行——minimized 事件
回调与托盘消息派发都在 UI 线程，满足该约束。
"""

import ctypes
import os
from ctypes import wintypes

__all__ = ["ensure", "remove"]

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

WM_APP = 0x8000
WM_TRAY = WM_APP + 1
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_CLOSE = 0x0010

NIM_ADD = 0
NIM_DELETE = 2
NIF_MESSAGE = 0x1
NIF_ICON = 0x2
NIF_TIP = 0x4

GWL_WNDPROC = -4
SW_RESTORE = 9

MF_SEPARATOR = 0x800
TPM_RIGHTBUTTON = 0x2
TPM_RETURNCMD = 0x100


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HANDLE),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", wintypes.HANDLE),
    ]


WNDPROCTYPE = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)


def _sig(fn, restype, *argtypes):
    fn.restype = restype
    fn.argtypes = list(argtypes)
    return fn


_sig(user32.ShowWindow, wintypes.BOOL, wintypes.HWND, ctypes.c_int)
_sig(user32.SetForegroundWindow, wintypes.BOOL, wintypes.HWND)
_sig(user32.LoadImageW, wintypes.HANDLE, wintypes.HINSTANCE, wintypes.LPCWSTR,
     wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT)
_sig(shell32.Shell_NotifyIconW, wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p)
_sig(user32.GetWindowLongPtrW, ctypes.c_ssize_t, wintypes.HWND, ctypes.c_int)
_sig(user32.SetWindowLongPtrW, ctypes.c_ssize_t, wintypes.HWND,
     ctypes.c_int, ctypes.c_ssize_t)
_sig(user32.CallWindowProcW, ctypes.c_ssize_t, ctypes.c_ssize_t, wintypes.HWND,
     wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
_sig(user32.DefWindowProcW, ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
     wintypes.WPARAM, wintypes.LPARAM)
_sig(user32.CreatePopupMenu, wintypes.HANDLE)
_sig(user32.AppendMenuW, wintypes.BOOL, wintypes.HANDLE, wintypes.UINT,
     wintypes.UINT, wintypes.LPCWSTR)
_sig(user32.TrackPopupMenu, wintypes.BOOL, wintypes.HANDLE, wintypes.UINT,
     ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, ctypes.c_void_p)
_sig(user32.DestroyMenu, wintypes.BOOL, wintypes.HANDLE)
_sig(user32.GetCursorPos, wintypes.BOOL, ctypes.POINTER(wintypes.POINT))
_sig(user32.PostMessageW, wintypes.BOOL, wintypes.HWND, wintypes.UINT,
     wintypes.WPARAM, wintypes.LPARAM)

_icon = None       # NOTIFYICONDATAW 实例（保持引用，remove 时用）
_orig_proc = None  # 原 WndProc 指针
_tray_proc = None  # 子类化回调（保持引用防 GC）
_tray_hwnd = 0
_restore_cb = None  # 恢复窗口回调（python 层注入，走 pywebview API 保持 WinForms 状态同步）


def _load_icon(ico_path):
    if not ico_path or not os.path.exists(ico_path):
        return 0
    return user32.LoadImageW(None, ico_path, 1, 0, 0, 0x10)  # IMAGE_ICON, LR_LOADFROMFILE


def _restore_window(hwnd):
    if _restore_cb is not None:
        try:
            _restore_cb()
            return
        except Exception:
            pass
    # 兜底：无回调时直接 Win32 恢复
    user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetForegroundWindow(hwnd)


@WNDPROCTYPE
def _wnd_proc(hwnd, msg, wparam, lparam):
    if msg == WM_TRAY:
        ev = lparam & 0xFFFF
        if ev in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
            _restore_window(hwnd)
            return 0
        if ev == WM_RBUTTONUP:
            _show_menu(hwnd)
            return 0
    if _orig_proc:
        return user32.CallWindowProcW(_orig_proc, hwnd, msg, wparam, lparam)
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def _show_menu(hwnd):
    menu = user32.CreatePopupMenu()
    user32.AppendMenuW(menu, 0, 1, "显示主界面")
    user32.AppendMenuW(menu, MF_SEPARATOR, 0, "")
    user32.AppendMenuW(menu, 0, 2, "退出")
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    # 右键菜单前必须先 SetForegroundWindow，否则菜单不消失
    user32.SetForegroundWindow(hwnd)
    cmd = user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD,
                                pt.x, pt.y, 0, hwnd, None)
    user32.DestroyMenu(menu)
    if cmd == 1:
        _restore_window(hwnd)
    elif cmd == 2:
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)


def ensure(hwnd, icon_path, title="AI Gateway", on_restore=None):
    """子类化主窗口接收托盘消息 + 添加托盘图标（幂等，须在 UI 线程调用）。

    on_restore：托盘点击恢复时调用的 python 回调（推荐走 pywebview 的
    show/restore，保持 WinForms 状态同步；直接 SW_RESTORE 会绕过 .NET
    状态缓存，导致之后的 minimize/maximize 失效）。
    """
    global _icon, _orig_proc, _tray_proc, _tray_hwnd, _restore_cb
    if on_restore is not None:
        _restore_cb = on_restore
    if not hwnd or _tray_hwnd == hwnd:
        return True
    _orig_proc = user32.GetWindowLongPtrW(hwnd, GWL_WNDPROC)
    _tray_proc = _wnd_proc
    user32.SetWindowLongPtrW(hwnd, GWL_WNDPROC,
                             ctypes.cast(_tray_proc, ctypes.c_void_p).value)
    nid = NOTIFYICONDATAW()
    nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
    nid.hWnd = hwnd
    nid.uID = 1
    nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
    nid.uCallbackMessage = WM_TRAY
    nid.hIcon = _load_icon(icon_path)
    nid.szTip = title[:127]
    ok = bool(shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)))
    if ok:
        _icon = nid
        _tray_hwnd = hwnd
    return ok


def remove():
    """移除托盘图标并还原 WndProc（退出时清理）。"""
    global _icon, _orig_proc, _tray_hwnd
    if _icon is not None:
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(_icon))
        _icon = None
    if _orig_proc is not None and _tray_hwnd:
        user32.SetWindowLongPtrW(_tray_hwnd, GWL_WNDPROC, _orig_proc)
        _orig_proc = None
    _tray_hwnd = 0
