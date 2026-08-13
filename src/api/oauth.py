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

    # 必须带完整客户端身份头：上游对无 UA 请求有风控（如 user_resource 的 10085），
    # 登录接口同样不能裸奔。build_headers() 无 token 时只带身份头。
    resp = session.post(url, headers=build_headers(), json={}, timeout=30)
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
        resp = session.get(url, headers=build_headers(), timeout=15)
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
                # 不在这里 pop pending！旧代码 _pending_oauth.pop(login_id, None) 是竞态根因：
                # setInterval 每 1.5s 触发一次 async 回调，如果某次 session.get 卡超过 1.5s，
                # 下一个回调会和它并发。A 拿到 token 并 pop 掉 pending 后，B 进 poll_token
                # 第 58 行 _pending_oauth.get 返回 None → raise「没有待处理的登录请求」。
                # 前端先弹错误再弹登录成功。
                # 改为只读：拿到 token 就返回，pending 留着，由 complete_oauth_and_save
                # 的 reset_pending() 在最终完成时统一清理。并发 poll 都能拿到同一个 token，
                # 不会有人因为 pending 被提前删而报错。
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


def reset_pending():
    """清空所有待处理的 OAuth 登录。

    每次 oauth_start 调用一次，保证「重复登录」「删了再登录」都从干净状态开始，
    避免上一次登录残留的 _pending_oauth 条目（成功后 poll_token 已 pop，但失败/超时/
    用户中途取消的会留下）干扰新一轮轮询。"""
    _pending_oauth.clear()


def fetch_account_info(access_token: str, state: str,
                       domain: Optional[str] = None) -> dict:
    session = get_session()
    url = f"{BASE_URL}/v2/plugin/login/account?state={state}"

    headers = build_headers(access_token, domain=domain)

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
