"""网关激活码校验（基于共享的 license_core 算法）。

校验纯靠算法 + 内置密钥，不依赖数据库/服务端。
本地仅缓存用户输入的码（避免每次启动重输）。

签发：用独立的「激活码签发工具」项目（相同 license_core 算法 + 相同密钥）。
改 SECRET 后所有旧码立即失效。
"""
import os
import time

from .license_core import generate as _generate, verify as _verify

# 产品标识 + 签名密钥（必须与签发工具里 cbcn2api 产品的密钥一致）
APP_ID = "cbcn2api"
SECRET = b"cbcn2api-7f3a-9e21-license-secret-2026"

_LICENSE_DIR = os.path.join(os.path.expanduser("~"), ".cbcn2api")
_LICENSE_FILE = os.path.join(_LICENSE_DIR, "license.dat")


def generate(expiry_ts: int) -> str:
    """签发（仅供本地调试，正式发码用签发工具）。"""
    return _generate(SECRET, expiry_ts, app=APP_ID)


def verify(code: str):
    """返回 (ok: bool, expiry: int|None, msg: str)。"""
    ok, payload, msg = _verify(SECRET, code, app=APP_ID)
    exp = int(payload.get("exp", 0)) if payload else None
    return ok, exp, msg


def save_code(code: str):
    os.makedirs(_LICENSE_DIR, exist_ok=True)
    with open(_LICENSE_FILE, "w", encoding="utf-8") as f:
        f.write(code.strip())


def load_code() -> str:
    try:
        with open(_LICENSE_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except (OSError, IOError):
        return ""


def status() -> dict:
    """当前授权状态（基于已缓存码做纯算法校验）。"""
    code = load_code()
    if not code:
        return {"licensed": False, "expiry": None, "message": "未激活"}
    ok, exp, msg = verify(code)
    return {"licensed": ok, "expiry": exp, "message": msg}
