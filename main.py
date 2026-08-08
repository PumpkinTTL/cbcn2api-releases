#!/usr/bin/env python3
import os
import sys
import tempfile
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


def _build_theme_inline():
    """生成内联主题引导脚本（打包版用）：读 theme.txt 设 data-theme。
    dev 版走 theme.js 文件机制；frozen 版 theme.js 无法可靠打包/写入（_MEIPASS
    临时解压目录），改内联进 HTML，彻底消除「首帧深色一闪」。
    """
    from src.storage import store
    theme = store.load_theme() or "light"
    if theme not in ("light", "dark"):
        theme = "light"
    return (
        "<script>(function(){var d='" + theme + "';"
        "try{var s=localStorage.getItem('theme');if(s==='light'||s==='dark')d=s;}catch(e){}"
        "document.documentElement.setAttribute('data-theme',d);window.__THEME__=d;})();</script>"
    )


def _prepare_frozen_html():
    """frozen：内联主题脚本 + <base> 指向 _MEIPASS，生成临时 HTML 加载。
    页面自身的相对资源（style.css/animations.css/vue.prod.js/icons）靠 base
    解析到解压目录，避免临时目录相对路径失效。
    版本号通过注入的 window.__APP_VERSION__ 由前端 JS 更新显示位置，
    绝不整体替换文本——否则会把更新日志里历史版本条目误改名。"""
    src = pathlib.Path(_resource(os.path.join("src", "gui", "index.html")))
    html = src.read_text(encoding="utf-8")
    try:
        from src.updater import APP_VERSION
        html = html.replace("<head>", f'<head><script>window.__APP_VERSION__="{APP_VERSION}";</script>', 1)
    except Exception:
        pass
    base = src.parent.as_uri() + "/"
    html = html.replace("<head>", f'<head><base href="{base}">', 1)
    html = html.replace('<script src="theme.js"></script>', _build_theme_inline())
    tmp = pathlib.Path(tempfile.gettempdir()) / "cbcn2api_gui.html"
    tmp.write_text(html, encoding="utf-8")
    return tmp.as_uri()


_GUI_HTML = _resource(os.path.join("src", "gui", "index.html"))
_HTML_URL = _prepare_frozen_html() if getattr(sys, 'frozen', False) else pathlib.Path(_GUI_HTML).as_uri()

_ICO_PATH = _resource("gateway.ico")


def _write_theme_bootstrap():
    """启动时根据 theme.txt 生成 src/gui/theme.js。

    解决「首次进入/刷新时暗色一闪而过」：旧流程要等 Vue mount + IPC 往返才设
    data-theme，期间用 :root 默认暗色渲染一帧。改为 theme.js 在 <head> 同步加载，
    CSS 应用前就把 data-theme 设对。

    生成逻辑在 store.regenerate_theme_js，save_theme 也会调它 ——
    用户切换主题时立即更新 theme.js，刷新页面就是新值（不会读到旧值把主题冲掉）。
    """
    try:
        from src.storage import store
        store.regenerate_theme_js(store.load_theme() or "light")
    except Exception as e:
        print(f"[theme] bootstrap 写入失败: {e!r}")


_write_theme_bootstrap()

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


def _mark_started():
    """GUI 主循环真正起来后写启动标记文件。

    更新脚本（VBS）靠这个文件判断新版本是否真正启动成功：
    bootloader 解压失败弹窗时 Python 尚未运行，不会写标记，
    不会像按进程名检测那样误判成功。
    """
    try:
        mark = os.path.join(tempfile.gettempdir(), "ai-gateway-check.txt")
        with open(mark, "w", encoding="utf-8") as f:
            f.write("started")
    except Exception:
        pass
    # 新实例已正常运行，兜底清理同目录残留的其他版本 exe
    try:
        from src.updater import cleanup_old_versions
        cleanup_old_versions()
    except Exception:
        pass


def main():
    api = GuiApi()

    window = webview.create_window(
        APP_TITLE,
        url=_HTML_URL,
        js_api=api,
        width=1407,
        height=880,
        min_size=(1000, 640),
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
        _mark_started,
        private_mode=False,
        debug=not getattr(sys, 'frozen', False),
    )


if __name__ == "__main__":
    main()
