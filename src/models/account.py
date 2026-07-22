import hashlib
import time
from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class Account:
    id: str = ""
    email: str = ""
    uid: Optional[str] = None
    nickname: Optional[str] = None
    enterprise_id: Optional[str] = None
    enterprise_name: Optional[str] = None
    tags: Optional[list[str]] = None

    access_token: str = ""
    refresh_token: Optional[str] = None
    token_type: str = "Bearer"
    expires_at: Optional[int] = None
    domain: Optional[str] = None

    plan_type: Optional[str] = None
    dosage_notify_code: Optional[str] = None
    dosage_notify_zh: Optional[str] = None
    dosage_notify_en: Optional[str] = None
    payment_type: Optional[str] = None

    quota_raw: Optional[dict] = None
    auth_raw: Optional[dict] = None
    profile_raw: Optional[dict] = None
    usage_raw: Optional[dict] = None

    status: Optional[str] = "normal"
    status_reason: Optional[str] = None
    quota_query_last_error: Optional[str] = None
    quota_query_last_error_at: Optional[int] = None

    last_checkin_time: Optional[int] = None
    checkin_streak: int = 0
    checkin_rewards: Optional[dict] = None

    created_at: int = 0
    last_used: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict) -> "Account":
        return Account(
            id=data.get("id", ""),
            email=data.get("email", ""),
            uid=data.get("uid"),
            nickname=data.get("nickname"),
            enterprise_id=data.get("enterprise_id"),
            enterprise_name=data.get("enterprise_name"),
            tags=data.get("tags"),
            access_token=data.get("access_token", ""),
            refresh_token=data.get("refresh_token"),
            token_type=data.get("token_type", "Bearer"),
            expires_at=data.get("expires_at"),
            domain=data.get("domain"),
            plan_type=data.get("plan_type"),
            dosage_notify_code=data.get("dosage_notify_code"),
            dosage_notify_zh=data.get("dosage_notify_zh"),
            dosage_notify_en=data.get("dosage_notify_en"),
            payment_type=data.get("payment_type"),
            quota_raw=data.get("quota_raw"),
            auth_raw=data.get("auth_raw"),
            profile_raw=data.get("profile_raw"),
            usage_raw=data.get("usage_raw"),
            status=data.get("status", "normal"),
            status_reason=data.get("status_reason"),
            quota_query_last_error=data.get("quota_query_last_error"),
            quota_query_last_error_at=data.get("quota_query_last_error_at"),
            last_checkin_time=data.get("last_checkin_time"),
            checkin_streak=data.get("checkin_streak", 0),
            checkin_rewards=data.get("checkin_rewards"),
            created_at=data.get("created_at", 0),
            last_used=data.get("last_used", 0),
        )

    def summary(self) -> dict:
        return {
            "id": self.id,
            "email": self.email,
            "uid": self.uid,
            "nickname": self.nickname,
            "enterprise_id": self.enterprise_id,
            "enterprise_name": self.enterprise_name,
            "tags": self.tags,
            "plan_type": self.plan_type,
            "dosage_notify_code": self.dosage_notify_code,
            "status": self.status,
            "last_checkin_time": self.last_checkin_time,
            "checkin_streak": self.checkin_streak,
            "created_at": self.created_at,
            "last_used": self.last_used,
        }

    @staticmethod
    def generate_id(seed: str) -> str:
        raw = seed.strip().lower()
        if not raw:
            raw = "unknown_user"
        return hashlib.md5(raw.encode()).hexdigest()[:16]

    @staticmethod
    def now_ts() -> int:
        return int(time.time())


class AccountIndex:
    def __init__(self):
        self.accounts: list[dict] = []

    def to_dict(self) -> dict:
        return {"accounts": self.accounts}

    @staticmethod
    def from_dict(data: dict) -> "AccountIndex":
        idx = AccountIndex()
        idx.accounts = data.get("accounts", [])
        return idx
