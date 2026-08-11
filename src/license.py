"""网关授权校验（远端 lic-admin 服务器 + 本地 license_core 兜底）。

授权开关与激活/验证由远端 lic-admin（license.bitlesu.com）控制：
  - 远端返回 enabled=false → 免授权直接可用
  - 远端返回 enabled=true  → 走激活/验证流程（机器码绑定）
  - 远端请求失败（断网/服务器宕机）→ 沿用本地 license_core 校验兜底，
    避免网络抖动锁死用户。

签发：lic-admin 后台生成激活码。
"""
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

from .license_core import generate as _generate, verify as _verify

# 产品标识 = lic-admin 后台的产品 ID（硬编码，查授权开关用，数字）
# APP 域标识 = license_core 密钥派生用（必须与签发工具一致，字符串）
APP_ID = 1
APP = "cbcn2api"
# 本地签名密钥（离线兜底用，与签发工具一致）。
# 必须与 lic-admin 的离线签发密钥一致（data/offline_secret.key / LIC_ADMIN_OFFLINE_SECRET）。
# 当前值 = 服务器 lic-admin 的 offline_secret.key（本地 lic-admin 已同步）。
SECRET = b"febfe7465b42c748bf60d43de5d595f58c9b8b6da3906fd3f35366fdcef36c81"

# 远端授权服务器（lic-admin）。
# 开发模式（非打包）默认连本地 http://127.0.0.1:8022；打包版默认 https://license.bitlesu.com。
# 环境变量 LIC_SERVER 始终优先，可覆盖。
if os.environ.get("LIC_SERVER"):
    _LIC_SERVER = os.environ["LIC_SERVER"]
elif getattr(sys, "frozen", False):
    _LIC_SERVER = "https://license.bitlesu.com"
else:
    _LIC_SERVER = "http://127.0.0.1:8022"

# 本地兜底缓存
_LICENSE_DIR = os.path.join(os.path.expanduser("~"), ".cbcn2api")
_LICENSE_FILE = os.path.join(_LICENSE_DIR, "license.dat")

# 离线激活码仅当前会话有效（打开一次），存内存不落盘 —— 关闭软件即作废，
# 重启需要新的码。同码严格防重用（DB offline_license_records 记录，不能二次激活）。
_session_offline_code = None

_TIMEOUT = 5


def generate(expiry_ts: int) -> str:
    """签发（仅供本地调试，正式发码用 lic-admin 后台）。"""
    return _generate(SECRET, expiry_ts, app=APP)


def machine_code() -> str:
    """采集本机唯一标识（稳定不变）。

    优先 Windows MachineGuid（HKLM\\SOFTWARE\\Microsoft\\Cryptography，
    系统安装时生成、重装系统才变，不随网卡/VPN/虚拟网卡漂移）。
    读取失败时退回网卡 MAC 哈希。
    不能主用 uuid.getnode()：它在 3.12 走 UuidCreateSequential，会取到
    虚拟网卡（VPN/Hyper-V）的 MAC 甚至无网卡时生成随机数，导致机器码
    漂移、用户被迫重新激活。
    """
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as k:
            guid, _ = winreg.QueryValueEx(k, "MachineGuid")
            if guid:
                raw = "MG-" + str(guid).strip()
                return "MID-" + hashlib.sha256(raw.encode()).hexdigest()[:16].upper()
    except Exception:
        pass
    raw = uuid.UUID(int=uuid.getnode()).hex
    return "MID-" + hashlib.sha256(raw.encode()).hexdigest()[:16].upper()


def _http_json(method: str, path: str, payload: dict = None, timeout: int = _TIMEOUT):
    url = _LIC_SERVER + path
    req = urllib.request.Request(url, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "AI-Gateway/" + str(APP_ID))
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    try:
        with urllib.request.urlopen(req, data=data, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8"))
        except Exception:
            body = {"message": str(e)}
        return e.code, body
    except Exception as e:
        raise ConnectionError(f"远端授权服务不可达: {e}")


def remote_license_enabled() -> bool:
    """查询远端：本产品（按硬编码 APP_ID）是否启用授权验证。
    返回 True/False；远端不可达时抛 ConnectionError，由调用方决定离线兜底。"""
    code, body = _http_json("GET", f"/api/v1/config?id={int(APP_ID)}", timeout=3)
    if code == 200 and isinstance(body, dict) and "enabled" in body:
        return bool(body["enabled"])
    raise ConnectionError(f"远端返回异常: code={code} body={body}")


def _is_offline_code(code: str) -> bool:
    """离线授权码格式：XXXX-XXXX-XXXX-XXXX（4 段 4 位）。
    在线激活码格式：XXXX-XXXXXXXXXXXX（前缀-12位hex）。"""
    parts = code.split("-")
    return len(parts) == 4 and all(len(p) == 4 for p in parts)


def verify(code: str):
    """验证已有授权（status 调用）。自动识别在线码 / 离线码。
    返回 (ok: bool, expiry: int|None, msg: str)。

    离线码：本地 offline_license_records 有记录 = 已授权（此前已激活），
    只检查是否过期；远端可达时顺带向服务端确认（已禁用则吊销）。
    在线码：走 /api/v1/verify，失败回退本地 license_core。
    """
    code = (code or "").strip().upper()
    if _is_offline_code(code):
        return _check_offline_status(code)
    return _verify_online(code)


def _check_offline_status(code: str):
    """验证离线码已有授权（纯本地算法验签，不联网）。"""
    ok, payload, msg = _verify(SECRET, code, app=APP)
    if ok:
        exp = int(payload.get("exp", 0)) if payload else None
        if exp and exp < time.time():
            return False, exp, "激活码已过期"
        return True, exp, "授权有效"
    return ok, None, msg


def _verify_offline(code: str):
    """离线授权码激活（纯本地算法验签，不联网）。
    严格防重用：同一码只能激活一次，本地 offline_license_records 有记录即拒绝。
    激活后持久化落盘 + 记录 machine_code（跨机器防重用兜底）。"""
    from src.storage import store
    # 防重用：已使用过的码一律拒绝（同码不能二次激活，防止反复白嫖）
    try:
        if store.is_offline_used(code):
            return False, None, "激活码已被使用"
    except Exception:
        pass
    # 本地 license_core 验签
    ok, payload, msg = _verify(SECRET, code, app=APP)
    if ok:
        exp = int(payload.get("exp", 0)) if payload else None
        if exp and exp < time.time():
            return False, exp, "激活码已过期"
        try:
            store.mark_offline_used(code, exp, machine_code())
        except Exception:
            pass
        return True, exp, "授权有效"
    return ok, None, msg


def _verify_online(code: str):
    """在线激活码验证。"""
    try:
        s, body = _http_json("POST", "/api/v1/verify",
                             {"code": code, "machine_code": machine_code(), "product_id": APP_ID})
        if s == 200 and body.get("ok"):
            exp = body.get("expires_at")
            return True, int(exp) if exp else None, "授权有效"
        return False, None, body.get("message") or body.get("detail") or "授权校验失败"
    except ConnectionError:
        return False, None, "无法连接服务器，请检查网络或使用离线密钥单次激活"


def activate(code: str):
    """激活（自动识别在线/离线码）。
    在线码：调 /api/v1/activate 绑定机器码，持久化到 license.dat。
    离线码：仅当服务器不可达时才允许（断网兜底）。单次会话有效 —— 存内存不落盘，
    关闭软件即作废，重启需新码。同码严格防重用（激活一次，DB 记录后不能再用）。
    返回 (ok: bool, expiry: int|None, msg: str)。"""
    global _session_offline_code
    code = (code or "").strip().upper()
    if _is_offline_code(code):
        # 服务器在线时禁止离线激活 —— 离线只是断网兜底，不是常规渠道
        try:
            remote_license_enabled()
            return False, None, "服务器在线，请使用在线激活码（离线激活仅在服务器不可达时可用）"
        except ConnectionError:
            pass  # 服务器不可达，允许离线兜底
        ok, exp, msg = _verify_offline(code)
        if ok:
            # 不落盘：单次会话有效，关闭即作废
            _session_offline_code = code
        return ok, exp, msg
    # 在线码：清掉会话级离线码
    _session_offline_code = None
    return _activate_online(code)


def _collect_device_info() -> dict:
    """采集设备信息（激活时上报，展示用）。"""
    import platform
    return {
        "name": os.environ.get("COMPUTERNAME", "") or platform.node(),
        "os": f"{platform.system()} {platform.release()}",
        "arch": platform.machine(),
        "python": platform.python_version(),
    }


def _activate_online(code: str):
    """在线激活码激活（绑定机器码）。"""
    try:
        s, body = _http_json("POST", "/api/v1/activate",
                             {"code": code, "machine_code": machine_code(), "product_id": APP_ID,
                              "device_info": _collect_device_info()})
        if s == 200 and body.get("ok"):
            exp = body.get("expires_at")
            save_code(code)
            return True, int(exp) if exp else None, "激活成功"
        return False, None, body.get("message") or body.get("detail") or "激活失败"
    except ConnectionError:
        return False, None, "无法连接服务器，请检查网络或使用离线密钥单次激活"


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
    """当前授权状态。
    离线码仅当前会话有效（内存），关闭即作废；在线码持久化（license.dat）。"""
    code = _session_offline_code or load_code()
    if not code:
        return {"licensed": False, "expiry": None, "message": "未激活"}
    ok, exp, msg = verify(code)
    return {"licensed": ok, "expiry": exp, "message": msg}
