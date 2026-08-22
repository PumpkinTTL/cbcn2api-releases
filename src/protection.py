"""客户端运行时防护（防逆向 / 防 hook / 防调试）。

威胁模型与对策（务实边界：内存级补丁无法完全防御，目标是大幅抬高门槛——
从"改个文件/挂个调试器就过"提升到"必须对着编译后的二进制做内存补丁"）：

1. 关键常量影子校验（防静态 patch + 防运行时篡改）：
   license.py 的 PUBKEY_HEX / APP_ID / 授权服务器地址在别处存一份混淆副本
   （XOR + 分段），校验时现场解混淆比对。破解者只改 license.py 里的明文
   常量（如把公钥换成自己的、把服务器指向假服务器）会被当场识破。
2. 调试器检测（防动态分析 / 断点 hook）：
   IsDebuggerPresent + CheckRemoteDebuggerPresent（本机+远程）。
   挂调试器断在验证函数上改返回值 → 拒绝放行。
3. 全部检测失败 = 一票否决授权（licensed=False），提示重新安装官方版本。

注意：检测只在授权链路调用（check_license / verify / heartbeat），
不碰网关数据面——性能零影响，误报不影响未授权功能的正常使用。
"""
import sys

# 混淆种子（改这里必须同步 _deobfuscate）
_XOR_KEY = 0x5A


def _deobfuscate(parts: list) -> str:
    """分段 + XOR 还原字符串：每个字符 code 异或回来。"""
    out = []
    for p in parts:
        for ch in p:
            out.append(chr(ord(ch) ^ _XOR_KEY))
    return "".join(out)


# ── 影子常量（与 license.py 的明文常量逐字符 XOR；两处必须同步改）──
# PUBKEY_HEX = "f01a1c1c7de7b8152b0d84272d087da78226569333a1e8634654d414cd1ec2f8"
_SHADOW_PUBKEY = [
    "<jk;k9k9m>?m8bko",
    "h8j>bnhmh>jbm>;m",
    "bhhlolciii;k?bli",
    "nlon>nkn9>k?9h<b",
]
# APP_ID = 1（'1' ^ 0x5A）
_SHADOW_APP_ID = "k"
# 打包版服务器 = https://license.bitlesu.com
_SHADOW_SERVER = [
    "2..*)`uu639?4)?t",
    "83.6?)/t957",
]


def integrity_ok() -> bool:
    """关键常量未被篡改？license.py 的明文值与影子副本不一致 = 被改。"""
    try:
        from src import license as lic
        if lic.PUBKEY_HEX != _deobfuscate(_SHADOW_PUBKEY):
            return False
        if str(lic.APP_ID) != _deobfuscate([_SHADOW_APP_ID]):
            return False
        # 服务器地址只对打包版校验（开发模式允许 LIC_SERVER 覆盖）
        if getattr(sys, "frozen", False) or "__compiled__" in globals():
            if lic._LIC_SERVER != _deobfuscate(_SHADOW_SERVER):
                return False
        return True
    except Exception:
        return False


def debugger_present() -> bool:
    """本机/远程调试器检测（仅 Windows；非 Windows 恒 False）。"""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        if k32.IsDebuggerPresent():
            return True
        found = ctypes.c_int(0)
        h = k32.GetCurrentProcess()
        if k32.CheckRemoteDebuggerPresent(h, ctypes.byref(found)) and found.value:
            return True
        return False
    except Exception:
        return False


def tampered() -> tuple:
    """统一防护裁决。返回 (被篡改: bool, 原因: str|None)——授权链路一票否决用。"""
    if not integrity_ok():
        return True, "客户端完整性校验失败，请重新安装官方版本"
    if debugger_present():
        return True, "检测到调试环境，无法验证授权，请关闭调试工具后重试"
    return False, None
