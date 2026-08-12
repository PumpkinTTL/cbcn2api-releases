"""Grok 凭证解析。

OAuth 返回的 token 集合里 id_token 是 OIDC JWT，email/userId 藏在 payload 里。
这里负责把原始 token 响应 + /v1/user 信息整理成 Account 字段，并算好绝对过期时间。
"""
import base64
import json
import time
from typing import Optional

from . import config


def _b64url_decode(segment: str) -> bytes:
    """JWT base64url 段解码（补齐 padding）。"""
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def decode_id_token_email(id_token: Optional[str]) -> Optional[str]:
    """从 id_token（OIDC JWT）的 payload 取 email。

    id_token 结构：header.payload.signature，payload 是 base64url 编码的 JSON。
    """
    if not id_token or id_token.count(".") < 2:
        return None
    try:
        payload = id_token.split(".")[1]
        data = json.loads(_b64url_decode(payload))
        email = data.get("email") or data.get("email_address")
        return str(email).strip() if email else None
    except Exception:
        return None


def decode_id_token_user_id(id_token: Optional[str]) -> Optional[str]:
    """从 id_token payload 取 sub / user_id。"""
    if not id_token or id_token.count(".") < 2:
        return None
    try:
        payload = id_token.split(".")[1]
        data = json.loads(_b64url_decode(payload))
        return str(data.get("sub") or data.get("user_id") or "").strip() or None
    except Exception:
        return None


def parse_credentials(
    token_response: dict,
    user_info: Optional[dict] = None,
) -> dict:
    """把 OAuth token 响应 + /v1/user 信息整理成扁平字段（供构建 Account）。

    返回：
        access_token, refresh_token, token_type, expires_at(绝对秒),
        scope, email, uid, nickname,
        auth_raw（原始 token 响应 + 订阅信息，落库兜底）
    """
    now = int(time.time())
    expires_in = token_response.get("expires_in")
    expires_at = (now + int(expires_in)) if expires_in else None

    id_token = token_response.get("id_token")
    email = (
        decode_id_token_email(id_token)
        or (user_info or {}).get("email")
        or (user_info or {}).get("email_address")
    )
    uid = (
        decode_id_token_user_id(id_token)
        or (user_info or {}).get("userId")
        or (user_info or {}).get("principalId")
    )
    first = (user_info or {}).get("firstName") or ""
    last = (user_info or {}).get("lastName") or ""
    nickname = f"{first} {last}".strip() or (user_info or {}).get("nickname") or None

    # auth_raw 兜底：id_token（刷新/身份用）/ scope / 订阅信息（hasGrokCodeAccess/subscriptionTier）
    auth_raw = {
        "id_token": id_token,
        "scope": token_response.get("scope"),
        "hasGrokCodeAccess": (user_info or {}).get("hasGrokCodeAccess"),
        "subscriptionTier": (user_info or {}).get("subscriptionTier"),
        "authMethod": "device_code",
    }

    return {
        "access_token": token_response.get("access_token", ""),
        "refresh_token": token_response.get("refresh_token"),
        "token_type": "xai-grok-cli",  # 自定义认证头值，build_headers 据此识别
        "expires_at": expires_at,
        "scope": token_response.get("scope"),
        "email": email,
        "uid": uid,
        "nickname": nickname,
        "auth_raw": auth_raw,
    }


def should_refresh(expires_at: Optional[int]) -> bool:
    """是否需要主动刷新（距过期不足 REFRESH_LEAD_SECONDS）。"""
    if not expires_at:
        return False
    return expires_at - int(time.time()) < config.REFRESH_LEAD_SECONDS
