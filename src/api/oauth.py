import time
import uuid
from typing import Optional

import requests

from src.api.client import BASE_URL, PLATFORM_CONFIG, build_headers, get_session

OAUTH_TIMEOUT = 600
OAUTH_POLL_INTERVAL = 1.5

_pending_oauth: dict = {}
_checkin_cache: dict = {}


def start_login(platform_key: str) -> dict:
    cfg = PLATFORM_CONFIG[platform_key]
    platform = cfg["platform"]

    session = get_session()
    url = f"{BASE_URL}/v2/plugin/auth/state?platform={platform}"

    resp = session.post(url, json={}, timeout=30)
    resp.raise_for_status()
    body = resp.json()

    data = body.get("data")
    if not data:
        raise ValueError(f"auth/state 响应缺少 data 字段: {list(body.keys())}")

    state = data.get("state")
    if not state:
        raise ValueError("auth/state 响应缺少 state")

    auth_url = data.get("authUrl") or data.get("auth_url") or data.get("url") or ""

    login_id = f"{cfg['login_prefix']}{uuid.uuid4().hex[:16]}"

    verification_uri = auth_url if auth_url else f"{BASE_URL}/login?state={state}"

    _pending_oauth[login_id] = {
        "state": state,
        "expires_at": int(time.time()) + OAUTH_TIMEOUT,
        "cancelled": False,
    }

    return {
        "login_id": login_id,
        "verification_uri": verification_uri,
        "verification_uri_complete": verification_uri,
        "expires_in": OAUTH_TIMEOUT,
        "interval_seconds": int(OAUTH_POLL_INTERVAL) + 1,
    }


def poll_token(login_id: str) -> Optional[dict]:
    pending = _pending_oauth.get(login_id)
    if not pending:
        raise ValueError("没有待处理的登录请求")
    if pending["cancelled"]:
        raise ValueError("登录已取消")
    if time.time() > pending["expires_at"]:
        _pending_oauth.pop(login_id, None)
        raise ValueError("登录超时")

    session = get_session()
    url = f"{BASE_URL}/v2/plugin/auth/token?state={pending['state']}"

    try:
        resp = session.get(url, timeout=15)
        body = resp.json()
    except Exception as e:
        return None

    code = body.get("code", -1)
    if code in (0, 200):
        data = body.get("data")
        if data:
            access_token = (
                data.get("accessToken") or data.get("access_token") or ""
            )
            if access_token:
                _pending_oauth.pop(login_id, None)
                return {
                    "access_token": access_token,
                    "refresh_token": data.get("refreshToken") or data.get("refresh_token"),
                    "expires_at": data.get("expiresAt") or data.get("expires_at"),
                    "domain": data.get("domain"),
                    "token_type": data.get("tokenType") or data.get("token_type") or "Bearer",
                    "auth_raw": data,
                }
    return None


def cancel_login(login_id: str):
    pending = _pending_oauth.get(login_id)
    if pending:
        pending["cancelled"] = True
        _pending_oauth.pop(login_id, None)


def fetch_account_info(access_token: str, state: str,
                       domain: Optional[str] = None) -> dict:
    session = get_session()
    url = f"{BASE_URL}/v2/plugin/login/account?state={state}"

    headers = {"Authorization": f"Bearer {access_token}"}
    if domain:
        headers["X-Domain"] = domain

    resp = session.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    body = resp.json()

    data = body.get("data", {})
    uid = data.get("uid")
    nickname = data.get("nickname")
    email = data.get("email", "") or nickname or uid or ""
    enterprise_id = data.get("enterpriseId") or ""
    enterprise_name = data.get("enterpriseName") or ""

    return {
        "uid": uid,
        "nickname": nickname,
        "email": email if email else (nickname or uid or "unknown"),
        "enterprise_id": enterprise_id if enterprise_id else None,
        "enterprise_name": enterprise_name if enterprise_name else None,
        "profile_raw": data,
    }
