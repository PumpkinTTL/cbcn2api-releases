"""Grok device code OAuth 流程（直连 auth.x.ai）。

与 cbcn2api 现有的中转 state OAuth（src/api/oauth.py）完全独立：
  - 现有：POST 自建中转 /v2/plugin/auth/state → 轮询 /v2/plugin/auth/token
  - grok：POST auth.x.ai/oauth2/device/code → 轮询 auth.x.ai/oauth2/token
         （带 referrer=grok-build，scope 含 conversations:read/write）

pending 状态机借鉴 src/api/oauth.py 的设计（login_id → device_code），但流程独立。
"""
import logging
import time
import uuid
from typing import Optional

import requests

from . import config
from .credentials import parse_credentials

logger = logging.getLogger(__name__)

# device code 流程相对中转 state 更慢（用户要开浏览器输 user_code），放宽超时
_TIMEOUT = 30
# 查询类请求（user/models/billing）超时短一些：避免 list_accounts 串行查询拖垮 RPC；
# device code / refresh 仍用 30s（用户在等登录/刷新）
_QUERY_TIMEOUT = 8
# login_id → {device_code, user_code, verification_uri, expires_at, cancelled, interval}
_pending: dict[str, dict] = {}


def _headers() -> dict:
    """device code / token 端点用的请求头（伪装官方 CLI）。"""
    return {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        # 抓包：device code 请求带 grok-pager + grok-shell 双 UA 标识
        "User-Agent": f"grok-pager/{config.CLIENT_VERSION} {config.USER_AGENT}",
    }


def start_login() -> dict:
    """发起 device code 流程，返回 login_id + 用户需访问的验证信息。"""
    body = {
        "client_id": config.CLIENT_ID,
        "scope": config.SCOPE,
    }
    # 官方 CLI 带 referrer=grok-build 标识 Grok Build 订阅来源
    if config.REFERRER:
        body["referrer"] = config.REFERRER

    resp = requests.post(config.DEVICE_CODE_URL, data=body, headers=_headers(), timeout=_TIMEOUT)
    if resp.status_code != 200:
        raise ValueError(f"device code 请求失败: HTTP {resp.status_code} {resp.text[:200]}")
    data = resp.json()

    device_code = data.get("device_code")
    if not device_code:
        raise ValueError(f"device code 响应缺 device_code: {data}")

    login_id = f"grok-{uuid.uuid4().hex[:16]}"
    _pending[login_id] = {
        "device_code": device_code,
        "user_code": data.get("user_code", ""),
        "verification_uri": data.get("verification_uri", ""),
        "verification_uri_complete": data.get("verification_uri_complete") or data.get("verification_uri", ""),
        "expires_at": int(time.time()) + int(data.get("expires_in", config.DEVICE_TIMEOUT)),
        "interval": int(data.get("interval", config.DEVICE_POLL_INTERVAL)),
        "cancelled": False,
    }

    return {
        "login_id": login_id,
        "user_code": data.get("user_code", ""),
        "verification_uri": data.get("verification_uri", ""),
        "verification_uri_complete": data.get("verification_uri_complete") or data.get("verification_uri", ""),
        "expires_in": data.get("expires_in", config.DEVICE_TIMEOUT),
        "interval": data.get("interval", config.DEVICE_POLL_INTERVAL),
    }


def poll_token(login_id: str) -> Optional[dict]:
    """轮询一次 token。未完成返回 None；完成返回 parse_credentials 结果。

    设计与 src/api/oauth.py.poll_token 一致：拿到 token 不立即 pop pending，
    由调用方完成保存后再 reset，避免并发轮询竞态。
    """
    pending = _pending.get(login_id)
    if not pending:
        raise ValueError("没有待处理的 Grok 登录请求")
    if pending["cancelled"]:
        raise ValueError("登录已取消")
    if time.time() > pending["expires_at"]:
        _pending.pop(login_id, None)
        raise ValueError("登录超时")

    body = {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "device_code": pending["device_code"],
        "client_id": config.CLIENT_ID,
    }
    try:
        resp = requests.post(config.TOKEN_URL, data=body, headers=_headers(), timeout=_TIMEOUT)
        data = resp.json()
    except Exception:
        return None

    err = data.get("error")
    # authorization_pending / slow_down 是正常的「用户还没授权」
    if err in ("authorization_pending", "slow_down"):
        return None
    if err:
        raise ValueError(f"Grok 授权失败: {err} ({data.get('error_description', '')})")

    access_token = data.get("access_token")
    if not access_token:
        return None

    # 拿到 token 后拉用户信息（非致命，失败也能用）
    user_info = None
    try:
        user_info = fetch_user(access_token)
    except Exception as e:
        logger.warning("[grok] fetch_user 失败（不影响登录）: %s", e)

    return parse_credentials(data, user_info)


def cancel_login(login_id: str):
    pending = _pending.get(login_id)
    if pending:
        pending["cancelled"] = True
        _pending.pop(login_id, None)


def reset_pending():
    """清空所有待处理的 Grok 登录（每次 start_login 前调一次，防残留干扰）。"""
    _pending.clear()


def fetch_user(access_token: str) -> Optional[dict]:
    """GET /v1/user?include=subscription 拉账号信息（userId / email / 订阅 tier）。

    带 ?include=subscription 才返回 subscriptionTier 等订阅字段（对齐 9router）。
    自定义认证头 x-xai-token-auth: xai-grok-cli（cli-chat-proxy 的鉴权约定）。
    """
    resp = requests.get(
        f"{config.BASE_URL}/user?include=subscription",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "User-Agent": config.USER_AGENT,
            "x-xai-token-auth": config.TOKEN_AUTH_HEADER_VALUE,
            "x-grok-client-version": config.CLIENT_VERSION,
        },
        timeout=_QUERY_TIMEOUT,
    )
    if resp.status_code != 200:
        return None
    return resp.json()


def fetch_models(access_token: str) -> list:
    """GET /v1/models 拉账号可用模型列表。

    关键：不同账号权限不同（有的 grok-build 订阅，有的只有 grok-4.6），
    写死 model 会 402。账号实际能用的模型以上游 /v1/models 为准。
    """
    try:
        resp = requests.get(
            f"{config.BASE_URL}/models",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
                "User-Agent": config.USER_AGENT,
                "x-xai-token-auth": config.TOKEN_AUTH_HEADER_VALUE,
                "x-grok-client-version": config.CLIENT_VERSION,
            },
            timeout=_QUERY_TIMEOUT,
        )
    except Exception:
        return []
    if resp.status_code != 200:
        return []
    data = resp.json()
    items = data.get("data", data) if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    return [m.get("id") if isinstance(m, dict) else str(m) for m in items]


def fetch_billing(access_token: str) -> dict:
    """GET /v1/billing?format=credits 拉账号额度（对齐 9router usage）。

    返回规范化字段（失败返回 {}）：
        on_demand_cap / on_demand_used / prepaid / monthly_limit /
        included_used / is_unified / period_end
    """
    try:
        resp = requests.get(
            f"{config.BASE_URL}/billing?format=credits",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
                "User-Agent": config.USER_AGENT,
                "x-xai-token-auth": config.TOKEN_AUTH_HEADER_VALUE,
                "x-grok-client-identifier": config.CLIENT_IDENTIFIER,
                "x-grok-client-version": config.CLIENT_VERSION,
                "x-grok-client-mode": "headless",
            },
            timeout=_QUERY_TIMEOUT,
        )
    except Exception:
        return {}
    if resp.status_code != 200:
        return {}
    data = resp.json() or {}
    cfg = data.get("config", data) if isinstance(data, dict) else {}

    def v(key, fallback=None):
        """解 protobuf-json 风格的 {val: n} 包裹。"""
        val = cfg.get(key)
        if isinstance(val, dict) and "val" in val:
            return val.get("val", fallback)
        return val

    return {
        "on_demand_cap": v("onDemandCap"),
        "on_demand_used": v("onDemandUsed"),
        "prepaid": v("prepaidBalance"),
        "monthly_limit": v("monthlyLimit"),
        "included_used": v("includedUsed"),
        "is_unified": cfg.get("isUnifiedBillingUser"),
        "period_end": cfg.get("billingPeriodEnd")
        or (cfg.get("currentPeriod") or {}).get("end"),
    }


def refresh_credentials(refresh_token: str) -> dict:
    """用 refresh_token 换新 token（直连 auth.x.ai/oauth2/token）。

    返回 parse_credentials 结果（含新的 access/refresh/expires_at）。
    """
    body = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": config.CLIENT_ID,
    }
    resp = requests.post(config.TOKEN_URL, data=body, headers=_headers(), timeout=_TIMEOUT)
    if resp.status_code != 200:
        raise ValueError(f"刷新失败: HTTP {resp.status_code} {resp.text[:200]}")
    data = resp.json()
    new_access = data.get("access_token")
    if not new_access:
        raise ValueError(f"刷新响应缺 access_token: {data}")
    return parse_credentials(data)
