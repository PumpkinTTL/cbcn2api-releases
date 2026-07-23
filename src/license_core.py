"""license_core — 可复用的激活码算法（短码 + 加盐密钥派生）。

码格式:
    XXXX-XXXX-XXXX-XXXX   （16 位 Crockford base32，排除易混字符 I/L/O/U）
内部布局 (80 bit):
    [到期 unix 秒 32 bit] [HMAC-SHA256 截断 48 bit]

防逆向:
    SECRET 不直接作为 HMAC 密钥，先经 PBKDF2-HMAC-SHA256 派生
    (盐 = 固定 pepper + app_id 域分离，10 万次迭代)。
    即便提取到裸 SECRET，仍需 pepper / 迭代数 / app_id / 算法才能伪造。
    注：纯离线授权无法 100% 防逆向；要更强保护需服务端校验。

复用: 此文件可原样拷贝到任何需要「签发/校验」激活码的项目。
"""
import hashlib
import hmac
import struct
import time

# Crockford base32 字母表（无 I L O U）
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_DECODE_MAP = {c: i for i, c in enumerate(CROCKFORD)}
for _a, _b in (("O", "0"), ("o", "0"), ("I", "1"), ("i", "1"),
               ("L", "1"), ("l", "1")):
    _DECODE_MAP[_a] = _DECODE_MAP[_b]

# 盐 + 迭代（密钥派生用）；改动后所有旧码失效
PEPPER = b"license-keygen-pepper-v1"
KDF_ITERS = 100000

# 有效期单位 -> 秒（月按 30 天计）
UNITS = {"seconds": 1, "minutes": 60, "hours": 3600, "days": 86400, "months": 2592000}
UNIT_LABELS = {"seconds": "秒", "minutes": "分钟", "hours": "小时", "days": "天", "months": "月"}


def to_seconds(amount, unit):
    if unit not in UNITS:
        raise ValueError(f"未知单位: {unit}")
    return int(amount) * UNITS[unit]


def _sb(s):
    return s.encode("utf-8") if isinstance(s, str) else s


def _derive_key(secret, app_id):
    """PBKDF2 派生真实 HMAC 密钥（加盐 + app 域分离）。"""
    return hashlib.pbkdf2_hmac(
        "sha256", _sb(secret), PEPPER + _sb(app_id or ""), KDF_ITERS, dklen=32
    )


def _b32_encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    return "".join(CROCKFORD[(n >> shift) & 0x1F] for shift in range(75, -1, -5))


def _b32_decode(s: str) -> bytes:
    s = (s or "").strip().replace("-", "").replace(" ", "").upper()
    if len(s) != 16:
        raise ValueError("长度不符")
    n = 0
    for c in s:
        if c not in _DECODE_MAP:
            raise ValueError(f"非法字符: {c}")
        n = (n << 5) | _DECODE_MAP[c]
    return n.to_bytes(10, "big")


def _format(raw16: str) -> str:
    return f"{raw16[0:4]}-{raw16[4:8]}-{raw16[8:12]}-{raw16[12:16]}"


def _pack(exp: int, sig: bytes) -> bytes:
    return struct.pack(">I", exp & 0xFFFFFFFF) + sig[:6]


def generate(secret, exp: int, app: str = "", **_kwargs) -> str:
    """签发激活码，返回 XXXX-XXXX-XXXX-XXXX 格式。"""
    exp = int(exp) & 0xFFFFFFFF
    exp_bytes = struct.pack(">I", exp)
    key = _derive_key(secret, app)
    sig = hmac.new(key, exp_bytes, hashlib.sha256).digest()  # 32 字节
    token = _pack(exp, sig)                                  # 10 字节 = 80 bit
    return _format(_b32_encode(token))


def verify(secret, code: str, app: str = None):
    """校验。返回 (ok: bool, payload: dict|None, msg: str)。

    app 必须与签发时一致（参与密钥派生，构成产品绑定）。
    """
    try:
        raw = _b32_decode(code)
    except Exception:
        return False, None, "激活码格式无效"
    if len(raw) != 10:
        return False, None, "激活码格式无效"
    exp_bytes, sig = raw[:4], raw[4:10]
    key = _derive_key(secret, app or "")
    expected = hmac.new(key, exp_bytes, hashlib.sha256).digest()[:6]
    if not hmac.compare_digest(sig, expected):
        return False, None, "激活码无效"
    exp = struct.unpack(">I", exp_bytes)[0]
    if exp < time.time():
        return False, {"exp": exp}, "激活码已过期"
    return True, {"exp": exp, "app": app}, "激活码有效"


def exp_of(code: str):
    """仅解析到期时间（不校验签名），用于展示。返回 unix 秒或 None。"""
    try:
        raw = _b32_decode(code)
        return struct.unpack(">I", raw[:4])[0]
    except Exception:
        return None
