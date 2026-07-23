#!/usr/bin/env python3
import os
import sys
import threading
import ctypes

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import webview
from src.gui.app import GuiApi
_GUI_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src", "gui", "index.html")
with open(_GUI_HTML, encoding="utf-8") as _f:
    HTML = _f.read()

APP_TITLE = "AI Gateway"
_ICO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gateway.ico")


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
        html=HTML,
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
