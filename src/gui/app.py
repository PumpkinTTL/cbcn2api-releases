import json
import os
import time
import threading
from pathlib import Path
from typing import Optional

from src.models.account import Account
from src.storage import store
from src.api import oauth as oauth_api
from src.api import account_api
from src.api import checkin as checkin_api
from src.api import quota as quota_api
from src.api.account_api import refresh_full_payload


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


class GuiApi:
    def __init__(self):
        self._oauth_callbacks = {}
        self._current_oauth_login_id = None

    # ========== Account Management ==========

    def list_accounts(self, platform: str) -> str:
        accounts = store.list_accounts(platform)
        return json.dumps([a.to_dict() for a in accounts])

    def get_account(self, platform: str, account_id: str) -> str:
        account = store.load_account(platform, account_id)
        if account:
            return json.dumps(account.to_dict())
        return json.dumps({"error": "账号不存在"})

    def delete_account(self, platform: str, account_id: str) -> str:
        store.remove_account(platform, account_id)
        return json.dumps({"success": True})

    def delete_accounts(self, platform: str, account_ids_json: str) -> str:
        ids = json.loads(account_ids_json)
        for aid in ids:
            store.remove_account(platform, aid)
        return json.dumps({"success": True})

    def import_from_json(self, platform: str, json_content: str) -> str:
        try:
            raw = json.loads(json_content)
        except json.JSONDecodeError as e:
            return json.dumps({"error": f"JSON 解析失败: {e}"})

        items = []
        if isinstance(raw, dict):
            if "accounts" in raw:
                items = raw["accounts"]
            elif "items" in raw:
                items = raw["items"]
            else:
                items = [raw]
        elif isinstance(raw, list):
            items = raw
        else:
            return json.dumps({"error": "JSON 必须是对象或数组"})

        if not items:
            return json.dumps({"error": "导入列表为空"})

        results = []
        for idx, item in enumerate(items):
            try:
                account = self._payload_to_account(item, platform)
                saved = store.upsert_account(platform, account)
                results.append(saved.to_dict())
            except Exception as e:
                return json.dumps({"error": f"第 {idx + 1} 条解析失败: {e}"})

        return json.dumps({"success": True, "accounts": results})

    def _payload_to_account(self, data: dict, platform: str) -> Account:
        access_token = (
            data.get("access_token")
            or data.get("accessToken")
            or data.get("token")
            or ""
        )
        if not access_token:
            raise ValueError("缺少 access_token")

        email = data.get("email") or ""
        uid = data.get("uid")
        nickname = data.get("nickname")
        enterprise_id = data.get("enterprise_id") or data.get("enterpriseId")
        enterprise_name = data.get("enterprise_name") or data.get("enterpriseName")
        refresh_token = data.get("refresh_token") or data.get("refreshToken")
        domain = data.get("domain")

        identity_seed = uid or email or "codebuddy_cn_user"
        account_id = Account.generate_id(identity_seed)

        dup_id = store.find_duplicate(platform, uid, email)
        if dup_id:
            account_id = dup_id

        now = Account.now_ts()
        account = Account(
            id=account_id,
            email=email,
            uid=uid,
            nickname=nickname,
            enterprise_id=enterprise_id,
            enterprise_name=enterprise_name,
            access_token=access_token,
            refresh_token=refresh_token,
            token_type=data.get("token_type") or data.get("tokenType") or "Bearer",
            expires_at=data.get("expires_at") or data.get("expiresAt"),
            domain=domain,
            plan_type=data.get("plan_type") or data.get("planType"),
            dosage_notify_code=data.get("dosage_notify_code") or data.get("dosageNotifyCode"),
            dosage_notify_zh=data.get("dosage_notify_zh") or data.get("dosageNotifyZh"),
            dosage_notify_en=data.get("dosage_notify_en") or data.get("dosageNotifyEn"),
            payment_type=data.get("payment_type") or data.get("paymentType"),
            quota_raw=data.get("quota_raw") or data.get("quotaRaw"),
            usage_raw=data.get("usage_raw") or data.get("usageRaw"),
            auth_raw=data.get("auth_raw") or data.get("authRaw"),
            profile_raw=data.get("profile_raw") or data.get("profileRaw"),
            tags=data.get("tags"),
            checkin_streak=data.get("checkin_streak") or data.get("checkinStreak") or 0,
            last_checkin_time=data.get("last_checkin_time") or data.get("lastCheckinTime"),
            status="normal",
            created_at=now,
            last_used=now,
        )
        return account

    def export_accounts(self, platform: str, account_ids_json: str) -> str:
        ids = json.loads(account_ids_json) if account_ids_json else []
        accounts = []
        for aid in ids:
            a = store.load_account(platform, aid)
            if a:
                accounts.append(a.to_dict())
        return json.dumps(accounts, ensure_ascii=False, indent=2)

    def update_tags(self, platform: str, account_id: str, tags_json: str) -> str:
        tags = json.loads(tags_json) if tags_json else []
        account = store.load_account(platform, account_id)
        if not account:
            return json.dumps({"error": "账号不存在"})
        account.tags = tags
        account.last_used = Account.now_ts()
        store.upsert_account(platform, account)
        return json.dumps(account.to_dict())

    # ========== OAuth Login ==========

    def oauth_start(self, platform: str) -> str:
        try:
            result = oauth_api.start_login(platform)
            self._current_oauth_login_id = result["login_id"]
            return json.dumps(result)
        except Exception as e:
            return json.dumps({"error": str(e)})

    def oauth_poll(self, login_id: str = None) -> str:
        lid = login_id or self._current_oauth_login_id
        if not lid:
            return json.dumps({"error": "没有待处理的登录"})
        try:
            result = oauth_api.poll_token(lid)
            if result is None:
                return json.dumps({"status": "polling"})
            state = oauth_api._pending_oauth.get(lid, {}).get("state", "")
            account_info = oauth_api.fetch_account_info(
                result["access_token"], state, result.get("domain")
            )
            result.update(account_info)
            return json.dumps({"status": "completed", "data": result})
        except Exception as e:
            return json.dumps({"error": str(e)})

    def oauth_cancel(self, login_id: str = None):
        lid = login_id or self._current_oauth_login_id
        if lid:
            oauth_api.cancel_login(lid)

    def complete_oauth_and_save(self, platform: str, token_data_json: str) -> str:
        data = json.loads(token_data_json)
        access_token = data["access_token"]
        uid = data.get("uid")
        email = data.get("email", "")
        nickname = data.get("nickname")
        enterprise_id = data.get("enterprise_id")
        enterprise_name = data.get("enterprise_name")
        domain = data.get("domain")
        refresh_token = data.get("refresh_token")
        token_type = data.get("token_type", "Bearer")
        expires_at = data.get("expires_at")
        auth_raw = data.get("auth_raw")
        profile_raw = data.get("profile_raw")

        identity_seed = uid or email or "codebuddy_cn_user"
        account_id = Account.generate_id(identity_seed)

        dup_id = store.find_duplicate(platform, uid, email)
        if dup_id:
            account_id = dup_id

        now = Account.now_ts()
        account = Account(
            id=account_id,
            email=email,
            uid=uid,
            nickname=nickname,
            enterprise_id=enterprise_id,
            enterprise_name=enterprise_name,
            access_token=access_token,
            refresh_token=refresh_token,
            token_type=token_type,
            expires_at=expires_at,
            domain=domain,
            auth_raw=auth_raw,
            profile_raw=profile_raw,
            status="normal",
            created_at=now,
            last_used=now,
        )

        saved = store.upsert_account(platform, account)
        return json.dumps(saved.to_dict())

    # ========== Stats ==========

    def get_stats(self, platform: str) -> str:
        accounts = store.list_accounts(platform)
        total = len(accounts)
        total_quota = 0
        total_used = 0
        for a in accounts:
            qr = a.quota_raw
            if not qr:
                continue
            ur = qr.get("userResource") or a.usage_raw
            if not ur:
                continue
            accts = (ur.get("data", {}).get("Response", {}).get("Data", {}).get("Accounts", [])
                     or ur.get("data", {}).get("Resources", []))
            for r in accts:
                cap = (
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
                total_quota += cap
                total_used += cap - remain
        checked_in = 0
        today_start = int(time.time()) // 86400 * 86400
        for a in accounts:
            lt = a.last_checkin_time
            if lt and lt >= today_start:
                checked_in += 1
        return json.dumps({
            "total_accounts": total,
            "total_quota": total_quota,
            "total_used": total_used,
            "checked_in_today": checked_in,
        })

    # ========== Token Operations ==========

    def refresh_token(self, platform: str, account_id: str) -> str:
        account = store.load_account(platform, account_id)
        if not account:
            return json.dumps({"error": "账号不存在"})

        try:
            payload, quota_error = refresh_full_payload(account)

            account.access_token = payload["access_token"]
            account.refresh_token = payload.get("refresh_token") or account.refresh_token
            account.expires_at = payload.get("expires_at") or account.expires_at
            account.domain = payload.get("domain") or account.domain
            account.dosage_notify_code = payload.get("dosage_notify_code") or account.dosage_notify_code
            account.dosage_notify_zh = payload.get("dosage_notify_zh") or account.dosage_notify_zh
            account.dosage_notify_en = payload.get("dosage_notify_en") or account.dosage_notify_en
            account.payment_type = payload.get("payment_type") or account.payment_type
            account.quota_raw = payload.get("quota_raw") or account.quota_raw
            account.usage_raw = payload.get("usage_raw") or account.usage_raw

            if quota_error:
                account.quota_query_last_error = quota_error
                account.quota_query_last_error_at = int(time.time() * 1000)
            else:
                account.quota_query_last_error = None
                account.quota_query_last_error_at = None

            account.last_used = Account.now_ts()
            saved = store.upsert_account(platform, account)
            return json.dumps(saved.to_dict())
        except Exception as e:
            return json.dumps({"error": str(e)})

    def refresh_all(self, platform: str) -> str:
        accounts = store.list_accounts(platform)
        success = 0
        for acc in accounts:
            result = json.loads(self.refresh_token(platform, acc.id))
            if "error" not in result:
                success += 1
        return json.dumps({"success": success, "total": len(accounts)})

    # ========== Check-in ==========

    def get_checkin_status(self, platform: str, account_id: str) -> str:
        account = store.load_account(platform, account_id)
        if not account:
            return json.dumps({"error": "账号不存在"})
        try:
            result = checkin_api.get_checkin_status(
                account.access_token, account.uid,
                account.enterprise_id, account.domain,
            )
            return json.dumps(result)
        except Exception as e:
            return json.dumps({"error": str(e)})

    def batch_checkin_status(self, platform: str) -> str:
        accounts = store.list_accounts(platform)
        today_start = int(time.time()) // 86400 * 86400
        updated = 0
        for acc in accounts:
            try:
                result = checkin_api.get_checkin_status(
                    acc.access_token, acc.uid,
                    acc.enterprise_id, acc.domain,
                )
                if result.get("today_checked_in"):
                    acc.last_checkin_time = today_start
                    store.upsert_account(platform, acc)
                    updated += 1
            except Exception:
                continue
        return json.dumps({"updated": updated, "total": len(accounts)})

    def checkin(self, platform: str, account_id: str) -> str:
        account = store.load_account(platform, account_id)
        if not account:
            return json.dumps({"error": "账号不存在"})
        try:
            result = checkin_api.perform_checkin(
                account.access_token, account.uid,
                account.enterprise_id, account.domain,
            )
            if result.get("success"):
                now = int(time.time())
                streak = result.get("streak_days")
                if streak is None:
                    streak = (account.checkin_streak or 0) + 1
                else:
                    streak = int(streak)
                account.last_checkin_time = now
                account.checkin_streak = streak
                if result.get("reward"):
                    account.checkin_rewards = result["reward"]
                elif result.get("credit"):
                    account.checkin_rewards = {"credit": result["credit"]}
                store.upsert_account(platform, account)
            return json.dumps(result)
        except Exception as e:
            return json.dumps({"error": str(e)})

    def checkin_all(self, platform: str) -> str:
        accounts = store.list_accounts(platform)
        results = {"success": 0, "failed": 0, "already": 0, "total": len(accounts)}
        now = int(time.time())
        for acc in accounts:
            try:
                result = checkin_api.perform_checkin(
                    acc.access_token, acc.uid,
                    acc.enterprise_id, acc.domain,
                )
                if result.get("success"):
                    streak = result.get("streak_days")
                    if streak is None:
                        streak = (acc.checkin_streak or 0) + 1
                    else:
                        streak = int(streak)
                    acc.last_checkin_time = now
                    acc.checkin_streak = streak
                    if result.get("reward"):
                        acc.checkin_rewards = result["reward"]
                    elif result.get("credit"):
                        acc.checkin_rewards = {"credit": result["credit"]}
                    store.upsert_account(platform, acc)
                    results["success"] += 1
                else:
                    msg = (result.get("message") or "").lower()
                    if "already" in msg or "checked" in msg:
                        results["already"] += 1
                    else:
                        results["failed"] += 1
            except Exception:
                results["failed"] += 1
        return json.dumps(results)

    # ========== Quota ==========

    def get_quota(self, platform: str, account_id: str) -> str:
        account = store.load_account(platform, account_id)
        if not account:
            return json.dumps({"error": "账号不存在"})
        try:
            result = quota_api.fetch_quota(
                account.access_token, account.uid,
                account.enterprise_id, account.domain,
            )
            return json.dumps(result)
        except Exception as e:
            return json.dumps({"error": str(e)})

    # ========== Import from Local (read VS Code state.vscdb) ==========

    def import_from_local(self, platform: str) -> str:
        try:
            data_dir = self._get_local_data_dir(platform)
            if not data_dir or not data_dir.exists():
                return json.dumps({"error": f"未找到 {platform} 客户端数据目录"})

            state_db = data_dir / "User" / "globalStorage" / "state.vscdb"
            if not state_db.exists():
                return json.dumps({"error": f"state.vscdb 不存在: {state_db}"})

            import sqlite3
            conn = sqlite3.connect(str(state_db))
            cursor = conn.cursor()

            secret_key = 'secret://{"extensionId":"tencent-cloud.coding-copilot","key":"planning-genie.new.accessTokencn"}'
            cursor.execute("SELECT value FROM ItemTable WHERE key = ?", (secret_key,))
            row = cursor.fetchone()
            conn.close()

            if not row:
                return json.dumps({"error": "未在本地客户端找到登录信息"})

            raw_value = row[0]

            try:
                parsed = json.loads(raw_value)
                token = self._extract_token(parsed)
            except json.JSONDecodeError:
                token = raw_value.strip()

            if not token:
                return json.dumps({"error": "无法解析 access token"})

            parts = token.split("+", 1)
            uid_from_token = parts[0].strip() if len(parts) > 1 else None
            access_token = parts[-1].strip()

            if not access_token:
                return json.dumps({"error": "access token 为空"})

            try:
                payload = account_api.build_payload_from_token(access_token)
            except Exception as e:
                payload = {
                    "access_token": access_token,
                    "email": "unknown",
                    "uid": uid_from_token,
                    "status": "normal",
                }

            if uid_from_token and not payload.get("uid"):
                payload["uid"] = uid_from_token

            identity_seed = payload.get("uid") or payload.get("email") or "unknown"
            account_id = Account.generate_id(identity_seed)

            now = Account.now_ts()
            account = Account(
                id=account_id,
                email=payload.get("email", "unknown"),
                uid=payload.get("uid"),
                nickname=payload.get("nickname"),
                enterprise_id=payload.get("enterprise_id"),
                enterprise_name=payload.get("enterprise_name"),
                access_token=access_token,
                refresh_token=payload.get("refresh_token"),
                token_type="Bearer",
                domain=payload.get("domain"),
                dosage_notify_code=payload.get("dosage_notify_code"),
                payment_type=payload.get("payment_type"),
                quota_raw=payload.get("quota_raw"),
                auth_raw=parsed if isinstance(raw_value, str) and self._is_json(raw_value) else None,
                usage_raw=payload.get("usage_raw"),
                status="normal",
                created_at=now,
                last_used=now,
            )

            saved = store.upsert_account(platform, account)
            return json.dumps(saved.to_dict())

        except Exception as e:
            return json.dumps({"error": str(e)})

    def _get_local_data_dir(self, platform: str) -> Optional[Path]:
        import sys
        if sys.platform == "win32":
            appdata = Path(os.environ.get("APPDATA", ""))
            if platform == "codebuddy_cn":
                return appdata / "CodeBuddy CN"
            else:
                return appdata / "WorkBuddy"
        elif sys.platform == "darwin":
            home = Path.home()
            name = "CodeBuddy CN" if platform == "codebuddy_cn" else "WorkBuddy"
            return home / "Library" / "Application Support" / name
        else:
            home = Path.home()
            name = "CodeBuddy CN" if platform == "codebuddy_cn" else "WorkBuddy"
            return home / ".config" / name

    def _extract_token(self, obj) -> Optional[str]:
        if isinstance(obj, str):
            return obj.strip() or None
        if isinstance(obj, list):
            for item in obj:
                result = self._extract_token(item)
                if result:
                    return result
        if isinstance(obj, dict):
            for key in ("token", "access_token", "accessToken"):
                v = obj.get(key)
                if v and isinstance(v, str) and v.strip():
                    return v.strip()
            auth = obj.get("auth")
            if isinstance(auth, dict):
                for key in ("accessToken", "access_token"):
                    v = auth.get(key)
                    if v and isinstance(v, str) and v.strip():
                        return v.strip()
            session = obj.get("session") or obj.get("data")
            if isinstance(session, dict):
                return self._extract_token(session)
        return None

    # ========== Settings ==========

    def get_theme(self) -> str:
        return store.load_theme()

    def set_theme(self, theme: str):
        store.save_theme(theme)
        return json.dumps({"ok": True})

    # ========== Switch Account (Inject to local client) ==========

    def _is_json(self, s: str) -> bool:
        try:
            json.loads(s)
            return True
        except (json.JSONDecodeError, TypeError):
            return False
