"""ed25519 验签（RFC 8032 参考实现，纯 Python，无第三方依赖）。

仅验签：客户端只需要内嵌公钥验证服务端响应签名（license.py _check_sig），
签名/私钥相关代码不进客户端（缩小可分析面）。
实现已经过 RFC 8032 §7.1 官方测试向量验证，与 cryptography / libsodium 互通。
验签约 50ms，仅在启动/启动代理的授权校验时调用，用户无感。
"""
import hashlib

p = 2**255 - 19
q = 2**252 + 27742317777372353535851937790883648493


def _sha512(m):
    return hashlib.sha512(m).digest()


def _inv(x):
    return pow(x, p - 2, p)


d = -121665 * _inv(121666) % p


def _sha512_modq(s):
    return int.from_bytes(_sha512(s), "little") % q


# 点用扩展坐标 (X, Y, Z, T) 表示：x=X/Z, y=Y/Z, xy=T/Z
def _pt_add(P, Q):
    A = (P[1] - P[0]) * (Q[1] - Q[0]) % p
    B = (P[1] + P[0]) * (Q[1] + Q[0]) % p
    C = 2 * P[3] * Q[3] * d % p
    D = 2 * P[2] * Q[2] % p
    E, F, G, H = B - A, D - C, D + C, B + A
    return (E * F % p, G * H % p, F * G % p, E * H % p)


def _pt_mul(s, P):
    Q = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            Q = _pt_add(Q, P)
        P = _pt_add(P, P)
        s >>= 1
    return Q


def _pt_equal(P, Q):
    if (P[0] * Q[2] - Q[0] * P[2]) % p != 0:
        return False
    if (P[1] * Q[2] - Q[1] * P[2]) % p != 0:
        return False
    return True


_sqrt_m1 = pow(2, (p - 1) // 4, p)


def _recover_x(y, sign):
    if y >= p:
        return None
    x2 = (y * y - 1) * _inv(d * y * y + 1)
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (p + 3) // 8, p)
    if (x * x - x2) % p != 0:
        x = x * _sqrt_m1 % p
    if (x * x - x2) % p != 0:
        return None
    if (x & 1) != sign:
        x = p - x
    return x


_g_y = 4 * _inv(5) % p
_g_x = _recover_x(_g_y, 0)
G = (_g_x, _g_y, 1, _g_x * _g_y % p)


def _pt_decompress(s):
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % p)


def verify(public: bytes, msg: bytes, signature: bytes) -> bool:
    """Ed25519 验签。public 32 字节、signature 64 字节。"""
    if len(public) != 32 or len(signature) != 64:
        return False
    A = _pt_decompress(public)
    if A is None:
        return False
    Rs = signature[:32]
    R = _pt_decompress(Rs)
    if R is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= q:
        return False
    h = _sha512_modq(Rs + public + msg)
    return _pt_equal(_pt_mul(s, G), _pt_add(R, _pt_mul(h, A)))
