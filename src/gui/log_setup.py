"""运行日志 + 全局异常捕获。

为什么需要
----------
进程未捕获异常（主线程 / 子线程 / JS）默认只打到 stderr，打包后用户看不见、
开发者也拿不到现场。「导出诊断信息」（app.export_diagnostics）会带上
runtime.log 末尾内容，形成「用户报错 → 一键导出 → 开发者看栈」闭环。

runtime.log 与数据库事件日志（proxy_logs 表）不同——这里只记 Python/JS
崩溃栈与致命错误，不记业务事件，体量小（RotatingFileHandler 1MB × 1 备份）。
"""
import logging
import sys
import threading
import traceback
from pathlib import Path

LOG_PATH = Path.home() / ".cbcn2api" / "runtime.log"
_LOGGER_NAME = "cbcn2api.runtime"


def setup_logging():
    """配置运行日志文件（RotatingFileHandler，1MB × 1 备份）。幂等。"""
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    if logger.handlers:  # 幂等：重复调用不叠加 handler
        return
    try:
        from logging.handlers import RotatingFileHandler
        handler = RotatingFileHandler(
            str(LOG_PATH), maxBytes=1_000_000, backupCount=1, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    except Exception:
        # 日志初始化失败不应阻断启动
        pass


def write_runtime_log(text: str, level: str = "ERROR"):
    """写一条运行日志（供 log_js_error 等直接调用）。"""
    logger = logging.getLogger(_LOGGER_NAME)
    getattr(logger, level.lower(), logger.error)(text)


def install_excepthooks():
    """装主线程 / 子线程未捕获异常钩子，把栈写到 runtime.log。"""

    def _fmt(exc_type, exc, tb):
        return "".join(traceback.format_exception(exc_type, exc, tb))

    def _main_hook(exc_type, exc, tb):
        # KeyboardInterrupt 是正常退出，不打日志
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        write_runtime_log(f"[UNHANDLED] 未捕获异常（主线程）\n{_fmt(exc_type, exc, tb)}")

    def _thread_hook(args):
        write_runtime_log(
            f"[UNHANDLED] 未捕获异常（线程 {args.thread.name}）\n"
            f"{_fmt(args.exc_type, args.exc_value, args.exc_traceback)}"
        )

    try:
        sys.excepthook = _main_hook
    except Exception:
        pass
    # threading.excepthook 仅 3.8+
    try:
        threading.excepthook = _thread_hook
    except Exception:
        pass
