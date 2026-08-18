"""网关授权校验（远端 lic-admin 服务器在线校验）。

授权开关与激活/验证由远端 lic-admin（license.bitlesu.com）控制：
  - 远端返回 enabled=false → 免授权直接可用
  - 远端返回 enabled=true  → 走激活/验证流程（机器码绑定）
  - 远端请求失败（断网/服务器宕机）→ 在线校验失败拒绝放行，无离线兜底。
    授权状态完全由服务端裁决，客户端不含任何签发/验签密钥。

签发：lic-admin 后台生成在线激活码。
（离线授权码机制已移除，历史实现见 git 分支 backup/offline-license-v1.1.2。）
"""
import hashlib
import json
import os
import secrets
import sys
import urllib.error
import urllib.request
import uuid

from .build_flags import INTERNAL_BUILD
from .ed25519 import verify as _ed25519_verify

# 产品标识 = lic-admin 后台的产品 ID（硬编码，查授权开关用，数字）
# 必须与 lic-admin 的 products 表 id 一致（AI Gateway = 100）
APP_ID = 100

# 响应验签公钥（Ed25519，32 字节 hex）。对应 lic-admin 的私钥
# data/signing_key.hex（或环境变量 LIC_ADMIN_SIGNING_KEY）。
# 公钥内嵌二进制是安全的：逆向提取公钥也伪造不出签名（需要服务端私钥）。
# 轮换密钥 = 服务端换 seed + 这里换公钥 + 重发客户端。
PUBKEY_HEX = "08c03eafb6b5cafde1c524dde4a178cc9d89cc9c4e386b98d9bc0f179d9f643b"

# 项目根 = src 的上一级（.env 放这里；按文件位置解析，与启动时 CWD 无关）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_dotenv() -> None:
    """极简 .env 加载（无依赖）：项目根 .env 的 KEY=VAL 塞进 os.environ（已有则不覆盖）。

    只解析「键=值」和行注释，不做引号/转义（GW_DEV / LIC_SERVER 这类开关足够用）。
    打包产物里此文件不存在，读到也没关系（_dev_bypass 由编译标志硬卡）。
    """
    try:
        with open(os.path.join(_PROJECT_ROOT, ".env"), encoding="utf-8") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if not line or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    except OSError:
        pass


_load_dotenv()


def _dev_bypass() -> bool:
    """开发豁免：仅「从源码运行」且 GW_DEV=1（环境变量或 .env）时跳过授权。

    打包后（Nuitka __compiled__ / PyInstaller frozen）永不触发 —— 与 main.py
    同款编译标志判断。豁免不含任何可伪造密钥，生产用户设 GW_DEV 或放 .env 均无效。
    """
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        return False
    return os.environ.get("GW_DEV") == "1"


def _internal_exempt() -> bool:
    """内部豁免版：build_internal.bat 打包时把 src/build_flags.py 的
    INTERNAL_BUILD 翻成 True 编译进二进制，跳过全部授权校验。
    豁免是编译期常量而非运行期开关 —— 正式版 exe 里恒为 False，
    设环境变量 / 改配置文件均无法触发。"""
    return INTERNAL_BUILD


# 远端授权服务器（lic-admin）。
# 开发模式（非打包）默认连本地 http://127.0.0.1:8022；打包版默认 https://license.bitlesu.com。
# LIC_SERVER 环境变量仅开发模式可覆盖 —— 打包版硬卡（frozen 检查）：
# 否则破解者设 LIC_SERVER 指向本地假服务器返回 enabled=false 即可免授权（零门槛秒破）。
if os.environ.get("LIC_SERVER") and not getattr(sys, "frozen", False):
    _LIC_SERVER = os.environ["LIC_SERVER"]
elif getattr(sys, "frozen", False):
    _LIC_SERVER = "https://license.bitlesu.com"
else:
    _LIC_SERVER = "http://127.0.0.1:8022"

# 本地缓存（在线激活码持久化）
_LICENSE_DIR = os.path.join(os.path.expanduser("~"), ".cbcn2api")
_LICENSE_FILE = os.path.join(_LICENSE_DIR, "license.dat")

_TIMEOUT = 5


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


def _canon_json(body: dict) -> bytes:
    """响应体规范序列化 —— 与 lic-admin _signed() 的 json.dumps 参数必须完全一致。"""
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _check_sig(body, nonce: str) -> dict:
    """校验服务端响应签名（Ed25519）。失败抛 ConnectionError（调用方按不可达处理）。

    防两种攻击：
      - 伪造响应（HTTPS MITM 改 body）：无私钥签不出有效签名
      - 重放旧响应：签名覆盖本次请求的随机 nonce，跨请求回放 nonce 不匹配
    返回去掉 _sig/_nonce 装饰字段后的原始响应体。"""
    if not isinstance(body, dict):
        raise ConnectionError("响应验签失败：响应格式异常")
    sig_hex = body.get("_sig")
    if not sig_hex or body.get("_nonce") != nonce:
        raise ConnectionError("响应验签失败：服务器版本过旧或连接被劫持")
    core = {k: v for k, v in body.items() if k not in ("_sig", "_nonce")}
    msg = (nonce + "|").encode("utf-8") + _canon_json(core)
    try:
        ok = _ed25519_verify(bytes.fromhex(PUBKEY_HEX), msg, bytes.fromhex(sig_hex))
    except Exception:
        ok = False
    if not ok:
        raise ConnectionError("响应验签失败：签名无效")
    return core


def remote_config() -> dict:
    """查询远端 config：返回 {"enabled": bool, "announcement": str|None}。
    公告随 config 下发，不依赖 verify/激活成功，启动第一跳即送达。

    验签只保护 enabled 字段（防 MITM 伪造 enabled=false 免授权）——公告是通知性
    内容，不需要防伪，直接从原始响应体取，验签失败也照常下发。开发/内部豁免版
    enabled 硬编码 False，但公告一样拉。"""
    dev = _dev_bypass() or _internal_exempt()
    try:
        nonce = secrets.token_hex(16)
        code, body = _http_json("GET", f"/api/v1/config?id={int(APP_ID)}&nonce={nonce}", timeout=3)
        if code != 200 or not isinstance(body, dict) or "enabled" not in body:
            raise ConnectionError(f"远端返回异常: code={code} body={body}")
        # 公告直接取，不参与验签（通知性内容，不需要防伪）
        announcement = body.get("announcement")
        if dev:
            return {"enabled": False, "announcement": announcement}
        # 正式版：验签 enabled（防 MITM 篡改授权开关），公告已取不受验签影响
        body = _check_sig(body, nonce)
        return {"enabled": bool(body["enabled"]), "announcement": announcement}
    except ConnectionError:
        if dev:
            return {"enabled": False, "announcement": None}
        raise


def remote_license_enabled() -> bool:
    """查询远端：本产品是否启用授权验证。向后兼容封装（丢弃 announcement）。
    新调用方应直接用 remote_config() 拿公告。"""
    return remote_config()["enabled"]


def _app_version() -> str:
    """当前客户端版本（上报服务端：在线追踪 + 最低版本门槛/黑名单校验）。
    取 src.updater.APP_VERSION；取不到（异常防御）返回空串，等价老客户端不上报。"""
    try:
        from src.updater import APP_VERSION
        return str(APP_VERSION)
    except Exception:
        return ""


def verify(code: str):
    """验证已有授权（status 调用）：调 /api/v1/verify。
    返回 (ok: bool, expiry: int|None, msg: str)。附加字段（公告/版本门槛）
    经 status() 的 extras 透出，不进本元组。"""
    code = (code or "").strip().upper()
    ok, exp, msg, _extras = _verify_online(code)
    return ok, exp, msg


def _verify_online(code: str):
    """在线激活码验证。返回 (ok, expiry, msg, extras)；
    extras 含服务端附加能力：announcement（公告），无则空 dict。"""
    try:
        nonce = secrets.token_hex(16)
        s, body = _http_json("POST", "/api/v1/verify",
                             {"code": code, "machine_code": machine_code(), "product_id": APP_ID,
                              "app_version": _app_version(), "nonce": nonce})
        if s == 200 and isinstance(body, dict):
            body = _check_sig(body, nonce)
            extras = {}
            if body.get("announcement"):
                extras["announcement"] = body["announcement"]
            if body.get("ok"):
                exp = body.get("expires_at")
                remain_days = None
                if exp:
                    import time as _t
                    remain_days = int((int(exp) - _t.time()) // 86400)
                    extras["expiry_days"] = remain_days
                return True, int(exp) if exp else None, "授权有效", extras
            return False, None, "授权校验失败", extras
        return False, None, body.get("message") or body.get("detail") or "授权校验失败", {}
    except ConnectionError as e:
        # 区分真话：网络不通 vs 验签失败（服务器换密钥或连接被劫持）——
        # 提示混在一起会把人引去查网络，方向全错
        msg = str(e)
        if "验签失败" in msg:
            return False, None, "签名异常，请更新到最新版本客户端", {}
        return False, None, "无法连接授权服务器，请检查网络后重试", {}


def heartbeat(code: str):
    """轻量在线心跳（授权有效期间每 5 分钟一拍，app.py 后台线程驱动）。
    服务端仅刷新在线时间/版本并回带最新公告；状态异常（禁用/过期/版本报废）403。
    响应经 Ed25519 签名，防运行途中 MITM 伪造心跳 200 掩盖吊销。
    返回 (state, msg, announcement)：state ∈ ok / rejected / unreachable。
    unreachable（断网/服务器宕机）不算失效 —— 心跳不做可用性惩罚，只在
    服务端「明确拒绝」时触发客户端锁定。"""
    code = (code or "").strip().upper()
    if not code:
        return "rejected", "未激活", None
    try:
        nonce = secrets.token_hex(16)
        s, body = _http_json("POST", "/api/v1/heartbeat",
                             {"code": code, "machine_code": machine_code(),
                              "app_version": _app_version(), "nonce": nonce}, timeout=5)
        if s == 200 and isinstance(body, dict):
            body = _check_sig(body, nonce)
            if body.get("ok"):
                return "ok", "OK", body.get("announcement") or None
        return "rejected", body.get("message") or body.get("detail") or "授权已失效", None
    except ConnectionError:
        return "unreachable", "授权服务器不可达", None


def activate(code: str):
    """激活在线激活码：调 /api/v1/activate 绑定机器码，持久化到 license.dat。
    返回 (ok: bool, expiry: int|None, msg: str)。"""
    code = (code or "").strip().upper()
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
        nonce = secrets.token_hex(16)
        s, body = _http_json("POST", "/api/v1/activate",
                             {"code": code, "machine_code": machine_code(), "product_id": APP_ID,
                              "device_info": _collect_device_info(),
                              "app_version": _app_version(), "nonce": nonce})
        if s == 200 and isinstance(body, dict):
            body = _check_sig(body, nonce)
            if body.get("ok"):
                exp = body.get("expires_at")
                save_code(code)
                return True, int(exp) if exp else None, "激活成功"
        return False, None, body.get("message") or body.get("detail") or "激活失败"
    except ConnectionError as e:
        msg = str(e)
        if "验签失败" in msg:
            return False, None, "签名异常，请更新到最新版本客户端"
        return False, None, "无法连接授权服务器，请检查网络后重试"


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
    """当前授权状态（读 license.dat 缓存的在线码，联网校验）。
    附加字段（有才带）：announcement（服务端公告）、expiry_days（剩余天数）、
    expiry_soon（3 天内到期，前端启动警示）。"""
    code = load_code()
    if not code:
        return {"licensed": False, "expiry": None, "message": "未激活"}
    ok, exp, msg, extras = _verify_online(code)
    st = {"licensed": ok, "expiry": exp, "message": msg}
    st.update(extras)
    # 临期标记：3 天内到期，前端启动时警示
    days = extras.get("expiry_days")
    if ok and days is not None and days <= 3:
        st["expiry_soon"] = True
    return st
