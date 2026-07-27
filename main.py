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

def _apply_window_chrome():
    """窗口出现后补上图标和圆角。

    frameless 窗口本身没有非客户区，干净无白边，不需要任何样式补丁。resize 走
    前端 delta + SetWindowPos（见 win_chrome.resize_delta），也不依赖窗口样式。
    所以这里只做两件不影响外观的事：设任务栏图标、显式声明 Win11 圆角。
    """
    if sys.platform != "win32":
        return

    from src.gui import win_chrome

    hwnd = win_chrome.find_main_hwnd(APP_TITLE)
    if not hwnd:
        return

    try:
        win_chrome.set_window_icon(hwnd, _ICO_PATH)
    except Exception:
        pass

    try:
        win_chrome.set_rounded_corners(hwnd)
    except Exception as e:
        print(f"[chrome] 圆角设置失败: {e!r}")


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
        frameless=True,
        easy_drag=False,
        # 必须关掉。shadow=True 时 pywebview 会调
        # DwmExtendFrameIntoClientArea(MARGINS 1,1,1,1)（winforms.py:169-184），
        # 把 DWM 框架往客户区里延伸 1px 并由 DWM 渲染 —— 这就是非最大化状态下
        # 顶部那条 1px 白线。
        # 圆角不受影响：win_chrome.set_rounded_corners() 已显式设置 DWMWCP_ROUND。
        shadow=False,
    )
    api.set_window(window)

    import atexit
    atexit.register(api.cleanup)

    threading.Thread(target=_apply_window_chrome, daemon=True).start()

    webview.start(
        private_mode=False,
        debug=not getattr(sys, 'frozen', False),
    )


if __name__ == "__main__":
    main()
