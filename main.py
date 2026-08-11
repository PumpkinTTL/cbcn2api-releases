#!/usr/bin/env python3
import os
import sys
import tempfile
import threading
import ctypes
import pathlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 统一打包环境标记：PyInstaller 会设置 sys.frozen，Nuitka 不设置（只注入 __compiled__）。
# 这里补齐，让 license/updater/store 等模块的 getattr(sys, "frozen", False) 判断
# 在两种构建方式下行为一致（Nuitka 走 frozen 分支：远程授权服务器/资源解压目录）。
if getattr(sys, "frozen", False) or "__compiled__" in globals():
    sys.frozen = True

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
    """获取资源路径，兼容打包环境（dev / PyInstaller / Nuitka）。

    PyInstaller: 资源在 sys._MEIPASS 解压目录。
    Nuitka: 无 _MEIPASS，数据文件随 exe 解压到临时目录（sys.executable 所在目录）。
    """
    if getattr(sys, 'frozen', False):
        base = getattr(sys, '_MEIPASS', None)
        if base is None:
            base = os.path.dirname(sys.executable)
        return os.path.join(base, name)
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

def _apply_window_chrome(window):
    """窗口出现后恢复系统行为 + 补图标（原生窗口改动在 UI 线程执行）。

    frameless 窗口本身没有非客户区，干净无白边。但去掉系统样式也丢掉了
    任务栏点击 roll-up、Win11 最小化/最大化动画、最大化贴边 ——
    apply_system_chrome 加回样式（NCCALCSIZE 隐藏渲染，外观不变），
    并接管圆角状态（最大化贴边无圆角、还原恢复圆角）。

    线程约束：SetWindowLongPtrW(GWL_WNDPROC) 子类化 + SWP_FRAMECHANGED 作用于
    窗口，必须在窗口所属线程（UI 线程）执行。本函数跑在后台线程（main() 里
    Thread 启动），所以用 Form.Invoke 把实际改动编组到 UI 线程——这和 pywebview
    自己跨线程操作（winforms.py 里大量 self.Invoke(Func[Type](...))）一致。
    不编组的话子类化会与 .NET Form.WndProc 消息泵竞态，表现为最大化盖任务栏 /
    圆角切换失效 / 任务栏点击不 roll-up 等时灵时不灵。
    """
    if sys.platform != "win32":
        return

    from src.gui import win_chrome

    hwnd = win_chrome.find_main_hwnd(APP_TITLE)
    if not hwnd:
        return

    form = getattr(window, "native", None)
    if form is None:
        return  # Form 尚未创建（find_main_hwnd 已确认窗口存在，理论不会到这）

    def _on_ui():
        try:
            win_chrome.apply_system_chrome(hwnd)
        except Exception as e:
            print(f"[chrome] 系统样式恢复失败: {e!r}")
        try:
            win_chrome.set_window_icon(hwnd, _ICO_PATH)
        except Exception:
            pass

    try:
        from System import Func, Type
        form.Invoke(Func[Type](_on_ui))
    except Exception as e:
        print(f"[chrome] Form.Invoke 失败，回退直调: {e!r}")
        _on_ui()


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
    # 仅更新后首次启动才清理同目录旧版本（有更新标记才清理，平时不扫，
    # 避免在构建产物目录里运行误删其他版本）
    try:
        updated_mark = os.path.join(tempfile.gettempdir(), "ai-gateway-updated.txt")
        if os.path.exists(updated_mark):
            from src.updater import cleanup_old_versions
            cleanup_old_versions()
            os.remove(updated_mark)
    except Exception:
        pass


def main():
    # 运行日志 + 全局异常捕获（主线程/子线程未捕获异常落 runtime.log，
    # 配合「导出诊断信息」形成报错-排错闭环）。初始化失败不阻断启动。
    try:
        from src.gui.log_setup import setup_logging, install_excepthooks
        setup_logging()
        install_excepthooks()
    except Exception:
        pass

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

    def _restore_from_tray():
        """托盘恢复窗口：走 pywebview show/restore 保持 WinForms 状态同步。

        不能用 SW_RESTORE 直接恢复——它会绕过 .NET 的 WindowState 缓存，
        .NET 仍认为窗口是 Minimized，之后 minimize/maximize 状态判断错乱，
        表现为缩小/全屏按钮失效。
        """
        try:
            window.show()
            window.restore()
        except Exception:
            pass

    # 关闭按钮走"最小化到托盘"语义（业界标准：X→托盘后台，最小化按钮→任务栏）。
    # 把图标路径和恢复回调注入 GuiApi，win_minimize_to_tray 在关闭时建托盘。
    api.set_tray_config(_ICO_PATH, _restore_from_tray)

    threading.Thread(target=lambda: _apply_window_chrome(window), daemon=True).start()

    webview.start(
        _mark_started,
        private_mode=False,
        debug=not getattr(sys, 'frozen', False),
    )


if __name__ == "__main__":
    main()
