from typing import Optional

from src.api.client import BASE_URL, build_headers, get_session
from src.api.quota import _fetch_dosage_notify, _fetch_payment_type, _fetch_user_resource
from src.models.account import Account


def build_payload_from_token(access_token: str, domain: Optional[str] = None) -> dict:
    session = get_session()
    url = f"{BASE_URL}/v2/plugin/accounts"

    headers = build_headers(access_token, domain=domain)
    resp = session.get(url, headers=headers, timeout=15)

    if resp.status_code != 200:
        return {
            "access_token": access_token,
            "email": "unknown",
            "uid": None,
            "status": "normal",
        }

    body = resp.json()

    accounts_list = body.get("data", {}).get("accounts", [])
    account_data = None
    for a in accounts_list:
        if a.get("lastLogin"):
            account_data = a
            break
    if not account_data and accounts_list:
        account_data = accounts_list[0]
    if not account_data:
        account_data = {}

    uid = account_data.get("uid")
    nickname = account_data.get("nickname")
    email = account_data.get("email", "") or nickname or uid or ""
    enterprise_id = account_data.get("enterpriseId") or ""
    enterprise_name = account_data.get("enterpriseName") or ""

    dosage = _fetch_dosage_notify(access_token, uid, enterprise_id, domain)
    payment = _fetch_payment_type(access_token, uid, enterprise_id, domain)
    user_resource = _fetch_user_resource(access_token, uid, enterprise_id, domain)

    is_forbidden = isinstance(user_resource, dict) and user_resource.get("_forbidden") is True
    if is_forbidden:
        user_resource = None

    dosage_data = dosage.get("data") if dosage else None
    payment_data = payment.get("data") if payment else None

    dosage_notify_code = None
    dosage_notify_zh = None
    dosage_notify_en = None
    if dosage_data:
        dosage_notify_code = dosage_data.get("dosageNotifyCode")
        dosage_notify_zh = dosage_data.get("dosageNotifyZh")
        dosage_notify_en = dosage_data.get("dosageNotifyEn")

    payment_type = None
    if payment_data:
        payment_type = payment_data if isinstance(payment_data, str) else payment_data.get("paymentType")

    quota_raw = {}
    if dosage:
        quota_raw["dosage"] = dosage
    if payment:
        quota_raw["payment"] = payment
    if user_resource:
        quota_raw["userResource"] = user_resource

    return {
        "uid": uid,
        "nickname": nickname,
        "email": email if email else (nickname or uid or "unknown"),
        "enterprise_id": enterprise_id if enterprise_id else None,
        "enterprise_name": enterprise_name if enterprise_name else None,
        "access_token": access_token,
        "token_type": "Bearer",
        "dosage_notify_code": dosage_notify_code,
        "dosage_notify_zh": dosage_notify_zh,
        "dosage_notify_en": dosage_notify_en,
        "payment_type": payment_type,
        "quota_raw": quota_raw if quota_raw else None,
        "profile_raw": account_data,
        "usage_raw": user_resource,
        "status": "banned" if is_forbidden else "normal",
    }




def refresh_token(access_token: str, refresh_token: str,
                  domain: Optional[str] = None) -> dict:
    session = get_session()
    url = f"{BASE_URL}/v2/plugin/auth/token/refresh"

    headers = build_headers(access_token, domain=domain)
    headers["X-Refresh-Token"] = refresh_token

    resp = session.post(url, headers=headers, json={}, timeout=15)

    if resp.status_code != 200:
        raise ValueError(f"刷新 token 失败 (HTTP {resp.status_code}): {resp.text[:200]}")

    body = resp.json()

    code = body.get("code", -1)
    if code not in (0, 200):
        msg = body.get("message") or body.get("msg") or "unknown"
        raise ValueError(f"刷新 token 失败 (code={code}): {msg}")

    data = body.get("data", {})
    return {
        "access_token": data.get("accessToken") or data.get("access_token") or access_token,
        "refresh_token": data.get("refreshToken") or data.get("refresh_token") or refresh_token,
        "expires_at": data.get("expiresAt") or data.get("expires_at"),
        "domain": data.get("domain") or domain,
    }


def refresh_full_payload(account: Account) -> tuple:
    new_at = account.access_token
    new_rt = account.refresh_token
    new_expires = account.expires_at
    new_domain = account.domain

    if account.refresh_token:
        try:
            result = refresh_token(account.access_token, account.refresh_token, account.domain)
            new_at = result["access_token"]
            new_rt = result["refresh_token"]
            new_expires = result.get("expires_at") or account.expires_at
            new_domain = result.get("domain") or account.domain
        except Exception as e:
            pass

    dosage = _fetch_dosage_notify(new_at, account.uid, account.enterprise_id, new_domain)
    payment = _fetch_payment_type(new_at, account.uid, account.enterprise_id, new_domain)
    user_resource = _fetch_user_resource(new_at, account.uid, account.enterprise_id, new_domain)

    is_forbidden = isinstance(user_resource, dict) and user_resource.get("_forbidden") is True
    if is_forbidden:
        user_resource = None

    quota_error = None
    if user_resource is None:
        quota_error = "账号已被封禁 (401/403)" if is_forbidden else "获取配额资源失败"

    dosage_data = dosage.get("data") if dosage else None
    payment_data = payment.get("data") if payment else None

    quota_raw = {}
    if dosage:
        quota_raw["dosage"] = dosage
    if payment:
        quota_raw["payment"] = payment
    if user_resource:
        quota_raw["userResource"] = user_resource

    payload = {
        "access_token": new_at,
        "refresh_token": new_rt,
        "expires_at": new_expires,
        "domain": new_domain,
        "dosage_notify_code": dosage_data.get("dosageNotifyCode") if dosage_data else account.dosage_notify_code,
        "dosage_notify_zh": dosage_data.get("dosageNotifyZh") if dosage_data else account.dosage_notify_zh,
        "dosage_notify_en": dosage_data.get("dosageNotifyEn") if dosage_data else account.dosage_notify_en,
        "payment_type": payment_data if isinstance(payment_data, str) else (payment_data.get("paymentType") if payment_data else account.payment_type),
        "quota_raw": quota_raw if quota_raw else account.quota_raw,
        "usage_raw": user_resource or account.usage_raw,
        "status": "banned" if is_forbidden else "normal",
    }

    return payload, quota_error
