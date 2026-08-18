from typing import Optional

from src.api.client import BASE_URL, api_request, build_headers, get_session

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
    """Status=0 才是有剩余的有效资源。
    实测：Status=0 的资源 remain>0（有额度）；Status=3 的资源全部 remain=0（已耗尽）。
    原先误把 3 也算 active，导致已用完的裂变包 used 全被累加，
    估算剩余额度被算成 0 —— 没到阈值就提前切号。"""
    s = _num(item.get("Status"))
    return s is not None and int(s) == 0


def _entry(item) -> dict:
    total = _pick(item, ["CycleCapacitySizePrecise", "CycleCapacitySize",
                         "CapacitySizePrecise", "CapacitySize"]) or 0.0
    remain = _pick(item, ["CycleCapacityRemainPrecise", "CycleCapacityRemain",
                          "CapacityRemainPrecise", "CapacityRemain"]) or 0.0
    used = max(0.0, total - remain)
    code = (item.get("PackageCode") or "").strip() or None
    s = _num(item.get("Status"))
    return {
        "packageCode": code,
        "packageName": (item.get("PackageName") or "").strip() or None,
        "status": int(s) if s is not None else None,
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


def parse_resources(accounts_raw: list, active_only: bool = True) -> list:
    """汇总套餐包。

    active_only=True（调度用）：只保留 Status=0 的有效包，已耗尽的 Status=3
    不计入 —— 否则耗尽包的 used 全被累加，估算剩余被压成 0，提前误切号。
    active_only=False（展示用）：保留 Status=0 和 Status=3 全量，给用户看
    「账号总共获得过多少额度、用了多少、还剩多少」—— 已耗尽的包也是账号
    获得过的额度，必须展示在总额度和详情里。
    """
    if active_only:
        active = [a for a in accounts_raw if _is_active(a)]
    else:
        active = [a for a in accounts_raw if _num(a.get("Status")) in (0, 3)]
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


def calc_totals(quota_raw: Optional[dict], usage_raw: Optional[dict] = None,
               active_only: bool = True) -> tuple:
    """汇算 total/used。

    active_only=True（默认，调度用）：只算 Status=0 有效包。
    active_only=False（展示用）：算 Status=0+3 全量，含已耗尽包的总额度。
    """
    ur = (quota_raw or {}).get("userResource") if quota_raw else None
    if not ur:
        ur = usage_raw
    if not ur:
        return 0, 0
    # 链式 .get 每级兜底 dict：上游报错时可能返回 {"data": null} / {"data": {"Response": null}}，
    # 中间任何一级是 None 都会导致 .get 对 None 调用抛 AttributeError。
    data = ur.get("data")
    if not isinstance(data, dict):
        return 0, 0
    resp = data.get("Response")
    if not isinstance(resp, dict):
        return 0, 0
    inner = resp.get("Data")
    if not isinstance(inner, dict):
        return 0, 0
    accounts = inner.get("Accounts")
    # Accounts 可能是 null（上游接口报错），兜底为空列表防 None 迭代
    if not isinstance(accounts, list):
        accounts = []
    resources = parse_resources(accounts, active_only=active_only)
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
    resources = parse_resources(accounts, active_only=False)

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
        resp = api_request(session, "POST", url, "拉用量", headers=headers, timeout=15)
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
        resp = api_request(session, "POST", url, "拉套餐", headers=headers, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


def _fetch_user_resource(access_token: str, uid: Optional[str],
                         enterprise_id: Optional[str],
                         domain: Optional[str]) -> Optional[dict]:
    """拉取账号全部套餐包（含分页）。

    实测上游单页最多 100 条，且账号裂变包会持续累积（每天 +1，旧的转 Status=3）。
    原先只取 PageNumber=1，超过 100 个包时后面的会被截断 —— 有效包被一堆耗尽包
    挤出第一页，额度算少 → 提前误切号。这里按 TotalCount 循环拉全。
    """
    session = get_session()
    url = f"{BASE_URL}/v2/billing/meter/get-user-resource"
    headers = build_headers(access_token, uid, enterprise_id, domain)
    headers["Accept-Language"] = "zh-CN,zh;q=0.9"

    begin = _time_range_begin()
    end = _time_range_end()
    page_size = 100
    all_accounts = []
    template = None  # 保留首次响应的完整结构作为返回骨架
    total_count = None

    for page in range(1, 200):  # 上限 200 页 = 20000 包，足够安全兜底
        body = {
            "PageNumber": page,
            "PageSize": page_size,
            "ProductCode": "p_tcaca",
            "Status": [0, 3],
            "PackageEndTimeRangeBegin": begin,
            "PackageEndTimeRangeEnd": end,
        }
        try:
            resp = api_request(session, "POST", url, "拉资源包", headers=headers, json=body, timeout=30)
        except Exception:
            return template if template else None
        if resp.status_code in (401, 403):
            return {"_forbidden": True}
        try:
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            return template if template else None

        if template is None:
            template = data
        inner = (((data or {}).get("data") or {}).get("Response") or {}).get("Data") or {}
        accts = inner.get("Accounts") or []
        all_accounts.extend(accts)

        # 首页拿到 TotalCount 决定还要拉几页
        if total_count is None:
            try:
                total_count = int(inner.get("TotalCount") or 0)
            except (ValueError, TypeError):
                total_count = 0

        # 本页不足 page_size 或已累计 >= total_count → 拉完
        if len(accts) < page_size or (total_count and len(all_accounts) >= total_count):
            break

    # 把累计的全量 Accounts 回填到模板结构里，保持调用方解析路径不变
    if template is not None and all_accounts:
        try:
            d = template.setdefault("data", {}).setdefault("Response", {}).setdefault("Data", {})
            d["Accounts"] = all_accounts
            if total_count is not None:
                d["TotalCount"] = total_count
        except Exception:
            pass
    return template


def _time_range_begin() -> str:
    import datetime
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _time_range_end() -> str:
    import datetime
    future = datetime.datetime.now() + datetime.timedelta(days=365 * 101)
    return future.strftime("%Y-%m-%d %H:%M:%S")
