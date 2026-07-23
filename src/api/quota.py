from typing import Optional

from src.api.client import BASE_URL, build_headers, get_session


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

    resources = []
    if user_resource:
        accounts = (
            user_resource.get("data", {}).get("Response", {}).get("Data", {}).get("Accounts", [])
            or user_resource.get("data", {}).get("Resources", [])
        )
    else:
        accounts = []

    def _num(v):
        if v is None:
            return 0
        if isinstance(v, (int, float)):
            return v
        try:
            return int(v)
        except (ValueError, TypeError):
            try:
                return float(v)
            except (ValueError, TypeError):
                return 0

    for r in accounts:
        total = (
            _num(r.get("CycleCapacitySizePrecise"))
            or _num(r.get("CycleCapacitySize"))
            or _num(r.get("CapacitySizePrecise"))
            or _num(r.get("CapacitySize"))
            or 0
        )
        remain = (
            _num(r.get("CycleCapacityRemainPrecise"))
            or _num(r.get("CycleCapacityRemain"))
            or _num(r.get("CapacityRemainPrecise"))
            or _num(r.get("CapacityRemain"))
            or 0
        )
        used = total - remain
        resources.append({
            "packageCode": r.get("PackageCode"),
            "packageName": r.get("PackageName"),
            "cycleStartTime": r.get("CycleStartTime"),
            "cycleEndTime": r.get("CycleEndTime"),
            "total": total,
            "remain": remain,
            "used": used,
            "isBasePackage": r.get("PackageCode") == "TCACA_code_009_0XmEQc2xOf",
        })

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
