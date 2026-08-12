"""Grok 服务层：GUI（pywebview.api 桥）与 HTTP 路由（/api/grok/*）共享的业务逻辑。

前端 iframe（grok.html）不再直连网关 HTTP API，而是经 postMessage RPC 桥 →
主 frame pywebview.api → 本服务层（与 CodeBuddy/WorkBuddy 同款调用链）。
HTTP 路由保留给无 GUI 的场景（脚本/调试）。
"""
import time

from src.storage import store
from src.models.account import Account

from . import config, oauth
from .provider import grok_pool


def account_brief(a: Account) -> dict:
    """账号卡片需要的精简字段（不含 token 明文）。"""
    auth_raw = a.auth_raw or {}
    return {
        "id": a.id,
        "email": a.email,
        "uid": a.uid,
        "nickname": a.nickname,
        "status": a.status,
        "subscription_tier": auth_raw.get("subscriptionTier"),
        "has_grok_code_access": auth_raw.get("hasGrokCodeAccess"),
        "expires_at": a.expires_at,
        "created_at": a.created_at,
        "last_used": a.last_used,
        "tags": a.tags or [],
    }


def list_accounts() -> list:
    return [account_brief(a) for a in store.list_accounts(config.PLATFORM_KEY)]


def oauth_start() -> dict:
    """发起 device code 登录，返回 {login_id, user_code, verification_uri_complete, ...}。"""
    oauth.reset_pending()
    return oauth.start_login()


def oauth_poll(login_id: str):
    """轮询一次 token。未完成返回 None；完成返回 parse_credentials 结果。"""
    return oauth.poll_token(login_id)


def oauth_cancel(login_id: str):
    oauth.cancel_login(login_id)


def complete_login(credentials: dict) -> dict:
    """保存登录成功账号（credentials 来自 oauth_poll）。返回 account_brief。"""
    access_token = (credentials or {}).get("access_token")
    if not access_token:
        raise ValueError("缺少 access_token")

    email = credentials.get("email") or ""
    uid = credentials.get("uid") or email or access_token[:16]
    account = Account(
        id=Account.generate_id(uid or email or access_token),
        email=email,
        uid=uid,
        nickname=credentials.get("nickname"),
        access_token=access_token,
        refresh_token=credentials.get("refresh_token"),
        token_type=credentials.get("token_type") or "xai-grok-cli",
        expires_at=credentials.get("expires_at"),
        auth_raw=credentials.get("auth_raw"),
        status="normal",
        created_at=int(time.time()),
    )
    saved = store.upsert_account(config.PLATFORM_KEY, account)
    grok_pool.reload()
    oauth.reset_pending()
    return account_brief(saved)


def refresh(account_id: str) -> dict:
    """手动刷新某账号 token。返回 {"expires_at": ...}。"""
    acc = store.load_account(config.PLATFORM_KEY, account_id) if account_id else None
    if not acc or not acc.refresh_token:
        raise ValueError("账号不存在或无 refresh_token")
    cred = oauth.refresh_credentials(acc.refresh_token)
    # 保留身份字段，仅更新 token
    acc.access_token = cred["access_token"]
    acc.refresh_token = cred["refresh_token"] or acc.refresh_token
    acc.expires_at = cred["expires_at"]
    new_auth = dict(acc.auth_raw or {})
    new_auth.update(cred.get("auth_raw") or {})
    acc.auth_raw = new_auth
    store.upsert_account(config.PLATFORM_KEY, acc)
    grok_pool.reload()
    return {"expires_at": acc.expires_at}


def delete(account_id: str):
    store.soft_delete_account(config.PLATFORM_KEY, account_id)
    grok_pool.reload()
