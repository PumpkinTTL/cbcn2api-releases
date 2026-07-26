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

_LICENSE_ENABLED = False



class GuiApi:
    _APP_TITLE = "AI Gateway"

    def __init__(self):
        self._oauth_callbacks = {}
        self._current_oauth_login_id = None
        self._window = None
        self._cached_hwnd = 0

    def set_window(self, window):
        self._window = window

    def _hwnd(self):
        """查找并缓存主窗口句柄（frameless 模式下用 Win32 控制窗口）。

        走 win_chrome.find_main_hwnd 以校验 PID，避免抓到别的进程里同名的窗口；
        句柄在窗口生命周期内不变，缓存掉每次点按钮的枚举开销。
        """
        if self._cached_hwnd:
            return self._cached_hwnd
        try:
            from src.gui import win_chrome
            # 窗口此刻已经在了，不必等 —— 超时给一点余量就够。
            self._cached_hwnd = win_chrome.find_main_hwnd(self._APP_TITLE, timeout=1.0)
        except Exception:
            self._cached_hwnd = 0
        return self._cached_hwnd

    def _is_maximized(self) -> bool:
        """查询窗口当前是否最大化。

        pywebview 的 window.maximized 只是建窗时的初始参数、不反映实时状态，
        所以状态仍靠 IsZoomed 读；但**动作**交给 pywebview 官方方法执行。
        """
        try:
            import ctypes
            hwnd = self._hwnd()
            return bool(hwnd and ctypes.windll.user32.IsZoomed(hwnd))
        except Exception:
            return False

    def win_minimize(self) -> str:
        # 用 pywebview 官方 API 而不是裸 ShowWindow：它内部走 Form.Invoke
        # 把调用编组到 UI 线程，而 js_api 方法本身跑在别的线程上。
        if self._window:
            self._window.minimize()
        return json.dumps({"ok": True})

    def win_toggle_max(self) -> str:
        if not self._window:
            return json.dumps({"ok": False, "maximized": False})
        was_max = self._is_maximized()
        if was_max:
            self._window.restore()   # WindowState = Normal，同样用于取消最大化
        else:
            self._window.maximize()
        # 回报真实状态，让前端图标跟着实际窗口走，而不是盲目 toggle class。
        return json.dumps({"ok": True, "maximized": not was_max})

    def win_close(self) -> str:
        if self._window:
            self._window.destroy()
        return json.dumps({"ok": True})

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
        content = (json_content or "").strip()
        raw = None
        try:
            raw = json.loads(content)
        except json.JSONDecodeError:
            # 不是合法 JSON — 当作裸 access_token 处理
            if content and content[0] not in "{[":
                raw = {"access_token": content}
            else:
                return json.dumps({"error": "无法解析：请粘贴有效的 Token 或 JSON"})

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

        if not uid and not nickname and not email:
            try:
                fetched = account_api.build_payload_from_token(access_token)
                uid = fetched.get("uid") or uid
                nickname = fetched.get("nickname") or nickname
                email = fetched.get("email") or email
                data = {**data, **fetched}
            except Exception:
                pass

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

    def export_accounts(self, platform: str) -> str:
        """弹原生保存对话框让用户选路径，写入后回报真实落盘位置。

        原先是把 JSON 抛给前端，用 Blob + <a download> 触发下载 —— 在 WebView2 里
        会静默落到系统下载目录，用户无从得知文件在哪，只看到一句「导出成功」。
        """
        accounts = store.list_accounts(platform)
        data = []
        for a in accounts:
            data.append({
                "id": a.id, "email": a.email, "uid": a.uid,
                "nickname": a.nickname, "enterprise_id": a.enterprise_id,
                "enterprise_name": a.enterprise_name,
                "access_token": a.access_token, "refresh_token": a.refresh_token,
                "token_type": a.token_type, "expires_at": a.expires_at,
                "domain": a.domain, "status": a.status,
            })

        if not data:
            return json.dumps({"error": "当前没有账号可导出"})

        payload = json.dumps(data, ensure_ascii=False, indent=2)

        if not self._window:
            return json.dumps({"error": "窗口未就绪"})

        default_name = f"accounts_{platform}_{time.strftime('%Y-%m-%d')}.json"
        try:
            import webview
            result = self._window.create_file_dialog(
                webview.FileDialog.SAVE,
                directory=str(Path.home() / "Downloads"),
                save_filename=default_name,
                file_types=("JSON 文件 (*.json)",),
            )
        except Exception as e:
            return json.dumps({"error": f"打开保存对话框失败: {e}"})

        # 用户取消时返回 None 或空序列
        if not result:
            return json.dumps({"cancelled": True})

        path = result if isinstance(result, str) else result[0]
        if not path.lower().endswith(".json"):
            path += ".json"

        try:
            Path(path).write_text(payload, encoding="utf-8")
        except Exception as e:
            return json.dumps({"error": f"写入失败: {e}"})

        return json.dumps({"ok": True, "path": path, "count": len(data)})

    def get_quota_threshold(self) -> str:
        from src.proxy.token_rotator import token_rotator
        return json.dumps({"threshold": token_rotator.get_threshold()})

    def set_quota_threshold(self, value: float) -> str:
        from src.proxy.token_rotator import token_rotator
        token_rotator.set_threshold(float(value))
        return json.dumps({"ok": True, "threshold": value})

    def get_all_stats(self, platform: str) -> str:
        from src.storage.store import list_account_stats
        stats = list_account_stats(platform)
        return json.dumps({"stats": stats})

    def reset_stats(self, platform: str = "", account_id: str = "") -> str:
        from src.storage.store import reset_account_stats
        reset_account_stats(platform, account_id)
        return json.dumps({"ok": True})

    # ========== Stats ==========

    def get_stats(self, platform: str) -> str:
        accounts = store.list_accounts(platform)
        total = len(accounts)
        total_quota = 0
        total_used = 0
        for a in accounts:
            t, u = quota_api.calc_totals(a.quota_raw, a.usage_raw)
            total_quota += t
            total_used += u
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

            if payload.get("status"):
                new_status = payload["status"]
                if new_status == "banned" or account.status not in ("disabled", "banned"):
                    account.status = new_status

            if quota_error:
                account.quota_query_last_error = quota_error
                account.quota_query_last_error_at = int(time.time() * 1000)
            else:
                account.quota_query_last_error = None
                account.quota_query_last_error_at = None

            account.last_used = Account.now_ts()
            saved = store.upsert_account(platform, account)
            try:
                store.reset_account_credit(platform, account_id)
            except Exception:
                pass
            try:
                from src.proxy.token_rotator import token_rotator
                token_rotator.reload(platform)
            except Exception:
                pass
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

    # ========== License ==========

    def check_license(self) -> str:
        if not _LICENSE_ENABLED:
            return json.dumps({"licensed": True, "expiry": None, "message": "OK"}, ensure_ascii=False)
        from src import license as lic
        st = lic.status()
        if st["expiry"]:
            st["expiry_str"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(st["expiry"]))
        return json.dumps(st, ensure_ascii=False)

    def activate(self, code: str) -> str:
        from src import license as lic
        ok, exp, msg = lic.verify(code)
        if ok:
            lic.save_code(code)
            expiry_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(exp)) if exp else ""
            return json.dumps({"success": True, "expiry": exp, "expiry_str": expiry_str, "message": msg}, ensure_ascii=False)
        return json.dumps({"success": False, "message": msg}, ensure_ascii=False)

    # ========== Settings ==========

    def get_theme(self) -> str:
        return store.load_theme()

    def set_theme(self, theme: str):
        store.save_theme(theme)
        return json.dumps({"ok": True})

    # ========== Proxy Gateway ==========

    def proxy_start(self, port: str, password: str) -> str:
        import threading
        import time
        import socket

        if _LICENSE_ENABLED:
            from src import license as lic
            st = lic.status()
            if not st.get("licensed"):
                return json.dumps({"error": st.get("message") or "授权无效，请先激活"})

        existing = json.loads(self.proxy_status())
        if existing.get("running"):
            return json.dumps({"error": "网关已在运行"})

        port_num = int(port) if port.strip() else 8001

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", port_num))
            sock.close()
        except OSError:
            return json.dumps({"error": f"端口 {port_num} 已被占用，请换一个端口"})

        # 设置环境变量（proxy_server 模块在 import 时读取）
        os.environ["CBCN_PROXY_PORT"] = str(port_num)
        os.environ["CBCN_PROXY_PASSWORD"] = password
        os.environ["CBCN_PROXY_PLATFORM"] = "workbuddy"

        try:
            from src.proxy.proxy_server import app as proxy_app, token_rotator, update_config
            import uvicorn

            update_config(port_num, password, "workbuddy")
            token_rotator.reload("workbuddy")

            config = uvicorn.Config(
                app=proxy_app,
                host="127.0.0.1",
                port=port_num,
                log_level="error",
                log_config=None,
            )
            server = uvicorn.Server(config)
            server.config.load()

            t = threading.Thread(target=server.run, daemon=True)
            t.start()
            time.sleep(1.0)

            if not server.started:
                return json.dumps({"error": "网关启动失败"})

            self._proxy_server = server
            self._proxy_port = port_num
            return json.dumps({"success": True, "port": port_num})
        except Exception as e:
            return json.dumps({"error": f"网关启动失败: {str(e)[:200]}"})

    def proxy_stop(self) -> str:
        server = getattr(self, "_proxy_server", None)
        if server:
            server.should_exit = True
            self._proxy_server = None
            try:
                from src.proxy.token_rotator import token_rotator
                token_rotator._active_count = 0
            except Exception:
                pass
            return json.dumps({"success": True})
        return json.dumps({"error": "网关未运行"})

    def proxy_status(self) -> str:
        server = getattr(self, "_proxy_server", None)
        running = server is not None and not server.should_exit
        return json.dumps({
            "running": running,
            "port": getattr(self, "_proxy_port", 8001),
        })

    def cleanup(self):
        server = getattr(self, "_proxy_server", None)
        if server:
            server.should_exit = True

    def open_external(self, url: str):
        import webbrowser
        webbrowser.open(url)

    def set_account_status(self, platform: str, account_id: str, status: str) -> str:
        """手动设置账号状态：normal / disabled。启用时清除运行时冷却。"""
        import json as _json
        acc = store.load_account(platform, account_id)
        if not acc:
            return _json.dumps({"ok": False, "error": "账号不存在"})
        try:
            from src.proxy.token_rotator import token_rotator
            if status == "disabled":
                if not token_rotator.on_disable(account_id):
                    return _json.dumps({"ok": False, "error": "至少保留一个可用账号"})
            acc.status = status
            store.upsert_account(platform, acc)
            if status == "normal":
                token_rotator.clear_disabled(account_id)
            token_rotator.reload(platform)
        except Exception:
            pass
        return _json.dumps({"ok": True})

    def set_priority_account(self, platform: str, account_id: str) -> str:
        """手动设置优先调度账号，持久化到 DB。"""
        import json as _json
        try:
            from src.proxy.token_rotator import token_rotator
            token_rotator.set_priority(account_id)
            store.save_setting("priority_account", account_id)
            return _json.dumps({"ok": True})
        except Exception as e:
            return _json.dumps({"ok": False, "error": str(e)})

    def get_priority_account(self) -> str:
        """获取持久化的优先账号 ID。"""
        import json as _json
        return _json.dumps({"priority": store.get_setting("priority_account", "")})

    def export_to_workbuddy(self, port: str = "", password: str = "") -> str:
        import pathlib

        port_num = int(port) if port and port.strip() else getattr(self, "_proxy_port", 8001)
        api_key = password.strip() if password else ""

        model_specs = {
            "hy3":            {"input": 192000,  "output": 64000,  "name": "Hy3"},
            "deepseek-v4-flash": {"input": 1000000, "output": 50000,  "name": "Deepseek-V4-Flash"},
            "deepseek-v4-pro":  {"input": 1000000, "output": 50000,  "name": "Deepseek-V4-Pro"},
            "glm-5.2":         {"input": 1000000, "output": 48000,  "name": "GLM-5.2"},
            "glm-5.1":         {"input": 200000,  "output": 48000,  "name": "GLM-5.1"},
            "glm-5v-turbo":    {"input": 200000,  "output": 38000,  "name": "GLM-5v-Turbo"},
            "minimax-m3":      {"input": 512000,  "output": 48000,  "name": "MiniMax-M3"},
            "kimi-k3-1":       {"input": 1000000, "output": 48000,  "name": "Kimi-K3"},
            "kimi-k2.7":       {"input": 256000,  "output": 32000,  "name": "Kimi-K2.7-Code"},
            "kimi-k2.6":       {"input": 256000,  "output": 32000,  "name": "Kimi-K2.6"},
            "auto":            {"input": 1000000, "output": 64000,  "name": "Auto"},
        }

        config = []
        for m in ["deepseek-v4-flash", "deepseek-v4-pro", "hy3",
                   "glm-5.2", "glm-5.1", "glm-5v-turbo",
                   "minimax-m3", "kimi-k3-1", "kimi-k2.7", "kimi-k2.6", "auto"]:
            spec = model_specs.get(m, {"input": 1000000, "output": 128000, "name": m})
            config.append({
                "id": spec["name"],
                "name": spec["name"],
                "vendor": "Custom",
                "url": f"http://localhost:{port_num}/v1",
                "apiKey": api_key,
                "supportsToolCall": True,
                "supportsImages": True,
                "supportsReasoning": True,
                "useCustomProtocol": False,
                "maxInputTokens": spec["input"],
                "maxOutputTokens": spec["output"],
                "reasoning": {
                    "supportedEfforts": ["low", "medium", "high", "xhigh"],
                    "canDisableThinking": False,
                },
            })

        candidates = [
            pathlib.Path.home() / ".workbuddy" / "models.json",
            pathlib.Path(os.environ.get("APPDATA", "")) / "WorkBuddy" / ".workbuddy" / "models.json",
        ]

        target = None
        for p in candidates:
            if p.parent.exists():
                target = p
                break
        if not target:
            target = candidates[0]
            target.parent.mkdir(parents=True, exist_ok=True)

        backup = None
        if target.exists():
            backup = target.with_suffix(".json.bak")
            try:
                backup.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
            except Exception:
                backup = None

        try:
            target.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
            return json.dumps({
                "success": True,
                "path": str(target),
                "count": len(config),
                "backup": str(backup) if backup else "",
            })
        except Exception as e:
            return json.dumps({"error": str(e)})

    def _is_json(self, s: str) -> bool:
        try:
            json.loads(s)
            return True
        except (json.JSONDecodeError, TypeError):
            return False

    # ——— 运行日志 ———

    def get_logs(self, platform: str = "workbuddy", limit: int = 200, offset: int = 0,
                 event: str = "", since: int = 0) -> str:
        from src.storage.store import list_logs
        logs = list_logs(platform, limit=limit, offset=offset, event=event, since=since)
        for log in logs:
            log["_time"] = time.strftime("%H:%M:%S", time.localtime(log["timestamp"]))
        return json.dumps({"logs": logs, "count": len(logs)})

    def clear_logs(self, platform: str = "", before: int = 0) -> str:
        from src.storage.store import clear_logs
        clear_logs(platform=platform, before=before)
        return json.dumps({"ok": True})

    def get_log_enabled(self) -> str:
        return json.dumps({"enabled": store.get_setting("log_enabled", "1") == "1"})

    def set_log_enabled(self, enabled: bool) -> str:
        store.save_setting("log_enabled", "1" if enabled else "0")
        return json.dumps({"ok": True, "enabled": enabled})
