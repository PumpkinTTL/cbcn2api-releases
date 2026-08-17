"""快捷方式 CDP 参数注入（WorkBuddy / ZCode）。

网关启动代理服务时，给应用的桌面/开始菜单快捷方式追加 CDP 调试参数，
用户照常双击桌面图标启动就自带调试端口（供 cdp_injector 注入额度横条）：
- WorkBuddy → --remote-debugging-port=9222
- ZCode     → --remote-debugging-port=9223
停代理/网关退出时还原原 Arguments，不留痕。

实现：优先 pywin32 COM（同一进程操作 .lnk，毫秒级，不拖慢网关启停）；
pywin32 不可用时降级 PowerShell subprocess（~1.5s，兜底）。
"""
import os
import subprocess
from pathlib import Path

# 各应用注入配置：lnk 匹配模式 + CDP 参数（端口错开，可同时开）
APP_CDP_CONFIGS = {
    "workbuddy": {
        "pattern": "WorkBuddy*.lnk",
        "args": "--remote-debugging-port=9222 --remote-allow-origins=*",
    },
    "zcode": {
        "pattern": "ZCode*.lnk",
        "args": "--remote-debugging-port=9223 --remote-allow-origins=*",
    },
}

# path(str) -> 注入前的原 Arguments（进程内记忆，restore 时还原；按应用隔离）
_bak: dict = {}

_LOOKUP_DIRS = [
    ("USERPROFILE", "Desktop"),
    ("PUBLIC", "Desktop"),
    ("APPDATA", r"Microsoft\Windows\Start Menu\Programs"),
    ("PROGRAMDATA", r"Microsoft\Windows\Start Menu\Programs"),
]

def _find_shortcuts(pattern: str) -> list:
    out = []
    for env, sub in _LOOKUP_DIRS:
        base = os.environ.get(env) or ""
        if not base:
            continue
        root = Path(base) / sub
        if not root.exists():
            continue
        try:
            for lnk in root.rglob(pattern):
                out.append(lnk)
        except Exception:
            continue
    return out


def _ps_quote(s: str) -> str:
    return s.replace("'", "''")


def _get_args(path: Path):
    # 每次在当前线程 CoInitialize + Dispatch：pywebview API 在不同后台线程调用，
    # 缓存 COM 对象跨线程使用会失败（COM 线程模型约束）
    try:
        import pythoncom
        pythoncom.CoInitialize()
        from win32com.client import Dispatch
        return str(Dispatch("WScript.Shell").CreateShortcut(str(path)).Arguments)
    except Exception:
        pass
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{_ps_quote(str(path))}');$s.Arguments"],
            capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def _set_args(path: Path, args: str) -> bool:
    try:
        import pythoncom
        pythoncom.CoInitialize()
        from win32com.client import Dispatch
        lnk = Dispatch("WScript.Shell").CreateShortcut(str(path))
        lnk.Arguments = args
        lnk.Save()
        return True
    except Exception:
        pass
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{_ps_quote(str(path))}');$s.Arguments='{_ps_quote(args)}';$s.Save()"],
            capture_output=True, timeout=10)
        return True
    except Exception:
        return False


def inject(app: str = "workbuddy") -> int:
    """给应用的所有快捷方式注入 CDP 参数（记录原值，幂等）。返回修改数。"""
    cfg = APP_CDP_CONFIGS.get(app)
    if not cfg:
        return 0
    changed = 0
    for p in _find_shortcuts(cfg["pattern"]):
        key = str(p)
        if key in _bak:
            continue
        orig = _get_args(p)
        if orig is None:
            continue
        _bak[key] = orig
        if cfg["args"] in (orig or ""):
            continue
        new_args = (orig + " " + cfg["args"]).strip() if orig else cfg["args"]
        if _set_args(p, new_args):
            changed += 1
    return changed


def restore() -> int:
    """还原所有已注入的快捷方式原参数。返回还原数。"""
    n = 0
    for key, orig in list(_bak.items()):
        if _set_args(Path(key), orig):
            n += 1
        _bak.pop(key, None)
    return n
