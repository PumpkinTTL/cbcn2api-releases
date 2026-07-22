from typing import Optional

from src.api.client import BASE_URL, build_headers, get_session


def get_checkin_status(access_token: str, uid: Optional[str] = None,
                       enterprise_id: Optional[str] = None,
                       domain: Optional[str] = None) -> dict:
    try:
        return _fetch_checkin_status(
            "/v2/billing/meter/checkin-activity-status",
            access_token, uid, enterprise_id, domain,
        )
    except Exception as activity_err:
        try:
            return _fetch_checkin_status(
                "/v2/billing/meter/checkin-status",
                access_token, uid, enterprise_id, domain,
            )
        except Exception as legacy_err:
            return {
                "today_checked_in": False,
                "active": False,
                "streak_days": 0,
                "error": f"activity=({activity_err}) legacy=({legacy_err})",
            }


def _fetch_checkin_status(path: str, access_token: str,
                          uid: Optional[str], enterprise_id: Optional[str],
                          domain: Optional[str]) -> dict:
    session = get_session()
    url = f"{BASE_URL}{path}"
    headers = build_headers(access_token, uid, enterprise_id, domain)

    resp = session.post(url, headers=headers, json={}, timeout=15)

    if resp.status_code != 200:
        raise ValueError(f"HTTP {resp.status_code}: {resp.text[:200]}")

    body = resp.json()

    code = body.get("code", -1)
    if code != 0:
        msg = body.get("message") or body.get("msg") or "unknown"
        raise ValueError(f"code={code}: {msg}")

    data = body.get("data", {})

    def get_bool(obj, *keys):
        for k in keys:
            v = obj.get(k)
            if isinstance(v, bool):
                return v
            if isinstance(v, int):
                return v != 0
            if isinstance(v, str):
                return v.lower() in ("true", "1")
        return None

    def get_int(obj, *keys):
        for k in keys:
            v = obj.get(k)
            if isinstance(v, int):
                return v
            if isinstance(v, str):
                try:
                    return int(v)
                except ValueError:
                    pass
        return None

    today_checked_in = get_bool(data, "today_checked_in", "todayCheckedIn") or False
    active = get_bool(data, "active", "Active")
    if active is None:
        active = True

    return {
        "today_checked_in": today_checked_in,
        "active": active,
        "streak_days": get_int(data, "streak_days", "streakDays") or 0,
        "daily_credit": get_int(data, "daily_credit", "dailyCredit") or 0,
        "today_credit": get_int(data, "today_credit", "todayCredit"),
        "next_streak_day": get_int(data, "next_streak_day", "nextStreakDay"),
        "is_streak_day": get_bool(data, "is_streak_day", "isStreakDay"),
        "checkin_dates": data.get("checkin_dates") or data.get("checkinDates"),
        "streak_bonus_days": get_int(data, "streak_bonus_days", "streakBonusDays"),
        "streak_bonus_credit": get_int(data, "streak_bonus_credit", "streakBonusCredit"),
    }


def perform_checkin(access_token: str, uid: Optional[str] = None,
                    enterprise_id: Optional[str] = None,
                    domain: Optional[str] = None) -> dict:
    session = get_session()
    url = f"{BASE_URL}/v2/billing/meter/daily-checkin"
    headers = build_headers(access_token, uid, enterprise_id, domain)
    headers["Accept"] = "application/json"

    resp = session.post(url, headers=headers, json={}, timeout=15)

    if resp.status_code != 200:
        return {
            "success": False,
            "message": f"HTTP {resp.status_code}: {resp.text[:200]}",
            "credit": None,
            "streak_days": None,
            "is_streak_day": None,
            "next_checkin_in": None,
            "reward": None,
        }

    body = resp.json()

    code = body.get("code", -1)
    if code != 0:
        msg = body.get("message") or body.get("msg") or "unknown error"
        return {
            "success": False,
            "message": msg,
            "credit": None,
            "streak_days": None,
            "is_streak_day": None,
            "next_checkin_in": None,
            "reward": None,
        }

    data = body.get("data", {})
    success = data.get("success", True)

    return {
        "success": success,
        "message": data.get("message"),
        "reward": data.get("reward"),
        "credit": data.get("credit") or data.get("today_credit"),
        "streak_days": data.get("streak_days"),
        "is_streak_day": data.get("is_streak_day"),
        "next_checkin_in": data.get("nextCheckinIn") or data.get("next_checkin_in"),
    }
