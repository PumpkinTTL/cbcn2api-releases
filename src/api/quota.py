from typing import Optional

from src.api.client import BASE_URL, build_headers, get_session

PKG_FREE = "TCACA_code_001_PqouKr6QWV"
PKG_PRO_MON = "TCACA_code_002_AkiJS3ZHF5"
PKG_PRO_YEAR = "TCACA_code_003_FAnt7lcmRT"
PKG_GIFT = "TCACA_code_006_DbXS0lrypC"
PKG_ACTIVITY = "TCACA_code_007_nzdH5h4Nl0"
PKG_FREE_MON = "TCACA_code_008_cfWoLwvjU4"
PKG_EXTRA = "TCACA_code_009_0XmEQc2xOf"

PRO_CODES = {PKG_PRO_MON, PKG_PRO_YEAR}
MERGED_BASE = {PKG_GIFT, PKG_FREE_MON}


def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip())
    except (ValueError, TypeError):
        return None


def _pick(item, keys):
    for k in keys:
        v = _num(item.get(k))
        if v is not None:
            return v
    return None


def _is_active(item) -> bool:
    s = _num(item.get("Status"))
    return s is not None and int(s) in (0, 3)


def _entry(item) -> dict:
    total = _pick(item, ["CycleCapacitySizePrecise", "CycleCapacitySize",
                         "CapacitySizePrecise", "CapacitySize"]) or 0.0
    remain = _pick(item, ["CycleCapacityRemainPrecise", "CycleCapacityRemain",
                          "CapacityRemainPrecise", "CapacityRemain"]) or 0.0
    used = max(0.0, total - remain)
    code = (item.get("PackageCode") or "").strip() or None
    return {
        "packageCode": code,
        "packageName": (item.get("PackageName") or "").strip() or None,
        "cycleStartTime": item.get("CycleStartTime"),
        "cycleEndTime": item.get("CycleEndTime"),
        "total": total,
        "remain": remain,
        "used": used,
        "isBasePackage": code != PKG_EXTRA,
    }


def _merge(items: list) -> Optional[dict]:
    if not items:
        return None
    merged = _entry(items[0])
    total = sum(_pick(i, ["CycleCapacitySizePrecise", "CycleCapacitySize",
                          "CapacitySizePrecise", "CapacitySize"]) or 0.0 for i in items)
    remain = sum(_pick(i, ["CycleCapacityRemainPrecise", "CycleCapacityRemain",
                           "CapacityRemainPrecise", "CapacityRemain"]) or 0.0 for i in items)
    merged["total"] = total
    merged["remain"] = remain
    merged["used"] = max(0.0, total - remain)
    return merged


def parse_resources(accounts_raw: list) -> list:
    active = [a for a in accounts_raw if _is_active(a)]
    if not active:
        return []

    def code_of(a):
        return (a.get("PackageCode") or "").strip()

    pro = [a for a in active if code_of(a) in PRO_CODES]
    extras = [a for a in active if code_of(a) == PKG_EXTRA]
    base_merge = [a for a in active if code_of(a) in MERGED_BASE]
    free = [a for a in active if code_of(a) == PKG_FREE]
    activity = [a for a in active if code_of(a) == PKG_ACTIVITY]

    ordered = []
    m = _merge(base_merge)
    if m:
        ordered.append(m)
    ordered += [_entry(a) for a in pro]
    ordered += [_entry(a) for a in activity]
    m = _merge(free)
    if m:
        ordered.append(m)
    m = _merge(extras)
    if m:
        ordered.append(m)

    return [e for e in ordered if e["total"] > 0 or e["remain"] > 0]


def calc_totals(quota_raw: Optional[dict], usage_raw: Optional[dict] = None) -> tuple:
    ur = (quota_raw or {}).get("userResource") if quota_raw else None
    if not ur:
        ur = usage_raw
    if not ur:
        return 0, 0
    accounts = (ur.get("data") or {}).get("Response", {}).get("Data", {}).get("Accounts", [])
    resources = parse_resources(accounts)
    total = sum(r["total"] for r in resources)
    used = sum(r["used"] for r in resources)
    return total, used


def fetch_quota(access_token: str, uid: Optional[str] = None,
                enterprise_id: Optional[str] = None,
                domain: Optional[str] = None) -> dict:
    dosage = _fetch_dosage_notify(access_token, uid, enterprise_id, domain)
    payment = _fetch_payment_type(access_token, uid, enterprise_id, domain)
    user_resource = _fetch_user_resource(access_token, uid, enterprise_id, domain)

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

    accounts = (
        (user_resource or {}).get("data", {}).get("Response", {}).get("Data", {}).get("Accounts", [])
    )
    resources = parse_resources(accounts)

    return {
        "dosage_notify_code": dosage_notify_code,
        "dosage_notify_zh": dosage_notify_zh,
        "dosage_notify_en": dosage_notify_en,
        "payment_type": payment_type,
        "resources": resources,
        "dosage_raw": dosage,
        "payment_raw": payment,
        "user_resource_raw": user_resource,
    }


def _fetch_dosage_notify(access_token: str, uid: Optional[str],
                         enterprise_id: Optional[str],
                         domain: Optional[str]) -> Optional[dict]:
    try:
        session = get_session()
        url = f"{BASE_URL}/v2/billing/meter/get-dosage-notify"
        headers = build_headers(access_token, uid, enterprise_id, domain)
        resp = session.post(url, headers=headers, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


def _fetch_payment_type(access_token: str, uid: Optional[str],
                        enterprise_id: Optional[str],
                        domain: Optional[str]) -> Optional[dict]:
    try:
        session = get_session()
        url = f"{BASE_URL}/v2/billing/meter/get-payment-type"
        headers = build_headers(access_token, uid, enterprise_id, domain)
        resp = session.post(url, headers=headers, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


def _fetch_user_resource(access_token: str, uid: Optional[str],
                         enterprise_id: Optional[str],
                         domain: Optional[str]) -> Optional[dict]:
    try:
        session = get_session()
        url = f"{BASE_URL}/v2/billing/meter/get-user-resource"
        headers = build_headers(access_token, uid, enterprise_id, domain)
        headers["Accept-Language"] = "zh-CN,zh;q=0.9"

        body = {
            "PageNumber": 1,
            "PageSize": 100,
            "ProductCode": "p_tcaca",
            "Status": [0, 3],
            "PackageEndTimeRangeBegin": _time_range_begin(),
            "PackageEndTimeRangeEnd": _time_range_end(),
        }

        resp = session.post(url, headers=headers, json=body, timeout=30)
        if resp.status_code in (401, 403):
            return {"_forbidden": True}
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


def _time_range_begin() -> str:
    import datetime
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _time_range_end() -> str:
    import datetime
    future = datetime.datetime.now() + datetime.timedelta(days=365 * 101)
    return future.strftime("%Y-%m-%d %H:%M:%S")
