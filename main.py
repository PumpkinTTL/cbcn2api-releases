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
    """窗口出现后补上原生装饰：图标、拉伸边框、去框架亮边、圆角。

    每一步各自 try —— 之前它们共享一个提前 return，窗口晚出现一点就会
    同时失效，而且被 except pass 吞掉、毫无提示。
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
        # 顺序有意义：先补样式拿到拉伸，再清掉非客户区去亮边，
        # 最后显式声明圆角（前两步改过样式，系统的圆角推断可能已变）。
        r1 = win_chrome.enable_resize_border(hwnd)
        r2 = win_chrome.suppress_nc_frame(hwnd)
        r3 = win_chrome.set_rounded_corners(hwnd)
        print(f"[chrome] resize={r1} nc_frame={r2} rounded={r3}")
    except Exception as e:
        print(f"[chrome] 窗口装饰失败: {e!r}")


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
        # 顶部那条 1px 白线。之前单独试 shadow=False 没效果，是因为当时更粗的
        # WS_THICKFRAME 非客户区还在，把这条细线盖住了。
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
