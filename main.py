#!/usr/bin/env python3
import os
import sys
import threading
import ctypes
import pathlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Windows 任务栏图标：设置 AppUserModelID 让系统识别为独立应用，
# 否则任务栏显示 python.exe 默认图标。必须在创建窗口前调用。
if sys.platform == "win32":
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("cbcn2api.gateway")
    except Exception:
        pass

import webview
from src.gui.app import GuiApi

APP_TITLE = "AI Gateway"

def _resource(name):
    """获取资源路径，兼容打包环境。"""
    if getattr(sys, 'frozen', False):
        return os.path.join(sys._MEIPASS, name)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), name)

_GUI_HTML = _resource(os.path.join("src", "gui", "index.html"))
_HTML_URL = pathlib.Path(_GUI_HTML).as_uri()

_ICO_PATH = _resource("gateway.ico")


def _set_window_icon():
    """Find the window by title and set its icon via Win32 API."""
    try:
        import time
        time.sleep(1.5)
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, APP_TITLE)
        if not hwnd:
            return
        WM_SETICON = 0x0080
        IMAGE_ICON = 1
        LR_LOADFROMFILE = 0x00000010
        hicon_lg = user32.LoadImageW(0, _ICO_PATH, IMAGE_ICON, 0, 0, LR_LOADFROMFILE)
        hicon_sm = user32.LoadImageW(0, _ICO_PATH, IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
        if hicon_lg:
            user32.SendMessageW(hwnd, WM_SETICON, 1, hicon_lg)
        if hicon_sm:
            user32.SendMessageW(hwnd, WM_SETICON, 0, hicon_sm)
    except Exception:
        pass


def main():
    api = GuiApi()

    window = webview.create_window(
        APP_TITLE,
        url=_HTML_URL,
        js_api=api,
        width=1200,
        height=820,
        min_size=(900, 600),
        resizable=True,
    )

    import atexit
    atexit.register(api.cleanup)

    threading.Thread(target=_set_window_icon, daemon=True).start()

    webview.start(
        private_mode=False,
        debug=not getattr(sys, 'frozen', False),
    )


if __name__ == "__main__":
    main()
