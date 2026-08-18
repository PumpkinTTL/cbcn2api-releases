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

# 授权开关：启动时从远端 lic-admin 按产品 ID 查询（LIC_SERVER）。
# 远端关闭授权 → 免授权直接用；远端开启 → 走激活码流程。
# 远端不可达时保守视为需要授权（后续在线校验同样失败，等价拒绝放行；无离线兜底）。
_LICENSE_ENABLED = None  # 运行时确定（True=需授权，False=免授权）
_CONFIG_ANNOUNCEMENT = None  # config 响应带回的公告（启动第一跳即送达，不依赖 verify）


def _resolve_license_enabled():
    """查询远端授权开关。返回 True（需授权）/ False（免授权）。结果缓存。
    公告随 config 下发 —— 顺手缓存到 _CONFIG_ANNOUNCEMENT，check_license 透传给前端。"""
    global _LICENSE_ENABLED, _CONFIG_ANNOUNCEMENT
    try:
        from src import license as lic
        cfg = lic.remote_config()
        _LICENSE_ENABLED = cfg["enabled"]
        _CONFIG_ANNOUNCEMENT = cfg.get("announcement")
    except Exception:
        # 远端不可达：无法确认开关，保守走授权（在线校验会失败，等价拒绝放行）
        _LICENSE_ENABLED = True
        _CONFIG_ANNOUNCEMENT = None
    return _LICENSE_ENABLED


# ── 授权心跳（服务端已上线 /api/v1/heartbeat，客户端每 5 分钟一拍）───────
# 价值：服务端实时掌握在线状态/版本分布；吊销/过期/版本报废在运行途中即时生效
# （不必等用户重启）。断网/服务器宕机不做惩罚（unreachable 只重试不锁定）——
# 可用性优先，真正的授权裁决仍在启动时的签名 verify。
_HB_INTERVAL = 300  # 秒，与服务端建议节奏一致
_hb_started = False
_hb_thread = None


def _push_license_event(window, payload: dict):
    """把心跳事件推给前端（evaluate_js 调 window.__licenseEvent 钩子）。"""
    try:
        if window is not None:
            window.evaluate_js(
                "window.__licenseEvent && window.__licenseEvent(" + json.dumps(payload, ensure_ascii=False) + ")"
            )
    except Exception:
        pass  # 窗口关闭/JS 未就绪期间的事件直接丢弃


def _heartbeat_loop(window):
    from src import license as lic
    from src.gui.log_setup import write_runtime_log
    while True:
        time.sleep(_HB_INTERVAL)
        code = lic.load_code()
        if not code:
            continue  # 未激活（免授权模式不会启动本线程；激活码被清则空转等重新激活）
        state, msg, announcement = lic.heartbeat(code)
        if state == "rejected":
            write_runtime_log(f"[license] 心跳被拒：{msg}", "WARN")
            _push_license_event(window, {"type": "revoked", "message": msg})
            return  # 授权已被服务端明确拒绝：停止心跳，前端锁定
        if state == "ok" and announcement:
            _push_license_event(window, {"type": "announcement", "content": announcement})


def start_license_heartbeat(window):
    """授权有效后启动心跳线程（幂等；免授权/内部豁免不启动）。"""
    global _hb_started, _hb_thread
    if _hb_started:
        return
    _hb_started = True
    _hb_thread = threading.Thread(target=_heartbeat_loop, args=(window,), daemon=True)
    _hb_thread.start()


def _parse_apikey_lines(content: str) -> tuple:
    """从粘贴文本里解析 API Key 账号行。支持两种格式（每行一个）：

    - 手机号----ck_xxx：手机号作显示名
    - ck_xxx（纯 key）：显示名留空，导入后从额度接口反查腾讯账号 Uin 自动补

    只认 ck_ 开头的 key（实测的长期凭证格式）；分隔符宽松匹配 2 个及以上连字符。
    返回 (entries, skipped)：entries = [(phone_or_empty, key), ...]，
    skipped = 未命中的非空行数。全文无命中时 entries 为空，调用方走原有
    JSON/裸 token 逻辑——老路径零影响（裸 token 是 JWT，不以 ck_ 开头，不误入）。
    """
    import re

    entries = []
    skipped = 0
    pattern = re.compile(r"^\s*(.*?)\s*-{2,}\s*(ck_\S+?)\s*$")
    for line in (content or "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = pattern.match(line)
        if m:
            entries.append((m.group(1), m.group(2)))
        elif line.startswith("ck_"):
            entries.append(("", line))
        else:
            skipped += 1
    return entries, skipped


def _lan_ips() -> list:
    """获取本机局域网 IPv4 地址（默认出口网卡 IP 优先，其余网卡去重补充）。

    UDP connect 不实际发包，仅走路由表取出口 IP——无外网环境同样可用；
    适合给局域网其他设备填网关地址用。
    """
    import socket as _socket

    ips = []
    try:
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for info in _socket.getaddrinfo(_socket.gethostname(), None, _socket.AF_INET):
            ip = info[4][0]
            # 过滤回环与 APIPA（网卡未拿到 DHCP 的自动配置地址，跨设备不可达）
            if ip.startswith("127.") or ip.startswith("169.254.") or ip in ips:
                continue
            ips.append(ip)
    except Exception:
        pass
    return ips


class GuiApi:
    _APP_TITLE = "AI Gateway"

    def __init__(self):
        self._oauth_callbacks = {}
        self._current_oauth_login_id = None
        self._window = None
        self._cached_hwnd = 0
        self._ico_path = ""
        self._on_tray_restore = None
        self._detect_lock = threading.Lock()
        self._detect_state = {
            "running": False, "finished": False, "total": 0, "done": 0,
            "enabled": 0, "skipped": 0, "banned": 0, "failed": 0,
            "last_account": "", "summary": "",
        }

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

    def set_tray_config(self, ico_path, on_restore):
        """注入托盘图标路径 + 恢复回调，供 win_minimize_to_tray 用。"""
        self._ico_path = ico_path
        self._on_tray_restore = on_restore

    def win_minimize_to_tray(self) -> str:
        """关闭按钮 → 最小化到系统托盘：隐藏主窗口 + 确保托盘图标。

        与 win_minimize 的区别：minimize 进任务栏（窗口仍可见）；本方法彻底隐藏
        窗口（仅托盘图标可见），用于"关闭即后台"语义。js_api 方法跑在 pywebview
        后台线程，tray.ensure 的原生子类化走 Form.Invoke 编组到 UI 线程。
        """
        try:
            from src.gui import tray, win_chrome
            hwnd = win_chrome.find_main_hwnd(self._APP_TITLE, timeout=3)
            if hwnd:
                try:
                    from System import Func, Type
                    self._window.native.Invoke(Func[Type](
                        lambda: tray.ensure(hwnd, self._ico_path, on_restore=self._on_tray_restore)
                    ))
                except Exception as e:
                    print(f"[tray] Form.Invoke 失败，回退直调: {e!r}")
                    tray.ensure(hwnd, self._ico_path, on_restore=self._on_tray_restore)
        except Exception as e:
            print(f"[tray] 最小化到托盘失败: {e!r}")
        if self._window:
            self._window.hide()
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
        # 销毁窗口前先停 uvicorn 并清理调度器状态，避免后台线程残留、端口占用。
        # cleanup 会做 should_exit=True + _active_count 归零，和 proxy_stop 对齐。
        self.cleanup(reason="win_close")
        try:
            from src.gui import tray
            tray.remove()
        except Exception:
            pass
        if self._window:
            self._window.destroy()
        return json.dumps({"ok": True})

    def resize_delta(self, direction: str, dx: int, dy: int) -> str:
        """前端 JS 拖动边缘时按帧调用：直接改窗口尺寸。

        这是 frameless + WebView2 架构下唯一可靠的 resize 方案 ——
        系统原生 sizing（WS_THICKFRAME 默认 hit-test）和发 WM_NCLBUTTONDOWN
        让系统进 sizing loop 都不工作：前者鼠标消息被 WebView2 子控件吃掉，
        后者 sizing loop 起不来（光标变但拖不动）。

        所以彻底绕开 Win32 sizing，改成前端自己算鼠标 delta，每帧调本方法，
        Python 端用 GetWindowRect + SetWindowPos 直接落尺寸。不依赖系统
        任何 sizing 状态，跨线程调用 SetWindowPos 是安全的。

        direction 决定 dx/dy 作用到哪些边以及符号方向；dx/dy 单位是逻辑像素，
        与 GetWindowRect 出来的物理像素一致（pywebview 6.2 + WebView2 在
        非 per-monitor DPI 下 scale=1；高 DPI 场景若出现缩放偏差，再补 scale 换算）。
        """
        hwnd = self._hwnd()
        if not hwnd:
            return json.dumps({"ok": False})
        from src.gui import win_chrome
        ok = win_chrome.resize_delta(hwnd, direction, dx, dy)
        return json.dumps({"ok": ok})

    # ========== Account Management ==========

    def list_accounts(self, platform: str) -> str:
        accounts = store.list_accounts(platform)
        out = [a.to_dict() for a in accounts]
        # 叠加 transient 冷却状态（内存级，不落库）——调度器 _disabled 里
        # reason=transient 且未过期的账号，响应里临时标记为 cooldown，
        # 前端显示「冷却中」而非「正常」，用户能看到账号在临时不可用。
        try:
            from src.proxy.token_rotator import token_rotator
            token_rotator.ensure_loaded(platform)
            st = token_rotator.status()
            cooldown_ids = {d["id"] for d in st.get("disabled", [])
                            if d.get("reason") == "transient" and d.get("until") and d["until"] > time.time()}
            for a in out:
                if a.get("id") in cooldown_ids and a.get("status") == "normal":
                    a["status"] = "cooldown"
        except Exception:
            pass
        return json.dumps(out)

    def get_account(self, platform: str, account_id: str) -> str:
        account = store.load_account(platform, account_id)
        if account:
            return json.dumps(account.to_dict())
        return json.dumps({"error": "账号不存在"})

    def delete_account(self, platform: str, account_id: str, soft: str = "1", note: str = "") -> str:
        # 最后号保护：删完若池中没有任何可用账号，拒绝删除。
        # 与 set_account_status 的禁用保护同一套判定（has_usable_besides）。
        is_soft = str(soft).lower() not in ("0", "false", "no")
        try:
            from src.proxy.token_rotator import token_rotator
            token_rotator.ensure_loaded(platform)
            if not token_rotator.has_usable_besides(account_id):
                return json.dumps({"success": False, "error": "这是最后一个可用账号，删除后网关将无法工作，已拒绝操作"})
        except Exception:
            pass
        if is_soft:
            # 软删除：status='deleted'，数据保留在库（回收站可见），可随时恢复
            # 每次删除动作生成一个批次号（同一次批量删除共用），回收站按批次整组操作
            # 备注仅软删除可填（批次存在才有地方挂），硬删除无批次直接忽略
            store.soft_delete_account(platform, account_id, int(time.time() * 1000), note)
        else:
            # 硬删除：物理删除 + tombstone 防快照并发回写复活
            store.remove_account(platform, account_id)
        # 同步调度器内存池：否则被删账号（尤其是当前调度的号）会留在 _accounts
        # 和 _current_id 里，get_next 仍会返回它发请求（幽灵调度）。
        # reload 会清掉无效 _current_id 并自动选下一个可用号。
        try:
            from src.proxy.token_rotator import token_rotator
            token_rotator.reload(platform)
        except Exception:
            pass
        return json.dumps({"success": True})

    def delete_accounts(self, platform: str, account_ids_json: str, soft: str = "1", note: str = "") -> str:
        ids = json.loads(account_ids_json)
        # 最后号保护：批量删完后若池中没有任何可用账号，拒绝整批删除。
        # 注意是「删完整批之后」而不是「每删一个查一次」—— 用户批量选中
        # 多个号时，只要删完还剩至少一个可用号就放行。
        if ids:
            try:
                from src.proxy.token_rotator import token_rotator
                token_rotator.ensure_loaded(platform)
                if not token_rotator.has_usable_besides(ids):
                    return json.dumps({"success": False, "error": "这是最后一个可用账号，删除后网关将无法工作，已拒绝操作"})
            except Exception:
                pass
        is_soft = str(soft).lower() not in ("0", "false", "no")
        # 同一次批量删除 = 同一批次号，回收站按批次整组恢复/彻底删除
        # 备注仅软删除可填（挂在本批次上），硬删除无批次忽略
        batch = int(time.time() * 1000) if is_soft else None
        for aid in ids:
            if is_soft:
                store.soft_delete_account(platform, aid, batch, note)
            else:
                store.remove_account(platform, aid)
        # 循环外只 reload 一次：每删一个都 reload 会反复重建内存池并竞争锁，
        # 没必要；删完一次性同步即可。
        try:
            from src.proxy.token_rotator import token_rotator
            token_rotator.reload(platform)
        except Exception:
            pass
        return json.dumps({"success": True})

    def list_deleted_accounts(self, platform: str) -> str:
        """回收站：软删除账号（含删除时间，数据仍在库，可恢复/彻底删除）。"""
        return json.dumps(store.list_deleted_accounts(platform), ensure_ascii=False)

    def restore_accounts(self, platform: str, account_ids_json: str) -> str:
        """回收站恢复：软删除账号去掉 deleted 标记、还原原状态并重新入池。
        恢复后清调度器运行时残留（冷却/banned 计数），否则恢复的号冷却未到期
        仍不可调度，或 _banned_fail 残留导致很快再次被标记封禁。"""
        try:
            ids = [str(i) for i in json.loads(account_ids_json or "[]")]
        except (ValueError, TypeError):
            return json.dumps({"error": "无效的账号列表"})
        if not ids:
            return json.dumps({"error": "没有选中账号"})
        from src.proxy.token_rotator import token_rotator
        restored = 0
        failed = 0
        for aid in ids:
            try:
                if store.restore_account(platform, aid):
                    restored += 1
                    token_rotator.clear_disabled(aid)
                else:
                    failed += 1
            except Exception:
                failed += 1
        if restored == 0:
            return json.dumps({"error": "没有可恢复的账号"})
        try:
            token_rotator.reload(platform)
        except Exception:
            pass
        return json.dumps({"success": True, "restored": restored, "failed": failed})

    def destroy_accounts(self, platform: str, account_ids_json: str) -> str:
        """回收站彻底删除：物理删除软删除账号（走硬删除路径，tombstone + 清统计）。"""
        try:
            ids = [str(i) for i in json.loads(account_ids_json or "[]")]
        except (ValueError, TypeError):
            return json.dumps({"error": "无效的账号列表"})
        if not ids:
            return json.dumps({"error": "没有选中账号"})
        destroyed = 0
        for aid in ids:
            try:
                store.remove_account(platform, aid)
                destroyed += 1
            except Exception:
                pass
        if destroyed == 0:
            return json.dumps({"error": "没有可删除的账号"})
        try:
            from src.proxy.token_rotator import token_rotator
            token_rotator.reload(platform)
        except Exception:
            pass
        return json.dumps({"success": True, "destroyed": destroyed})

    def restore_batch(self, platform: str, batch: str) -> str:
        """回收站按批次恢复：一次删除的一整组账号一起恢复，还原各自原状态。"""
        try:
            batch = int(batch)
        except (ValueError, TypeError):
            return json.dumps({"error": "无效的批次号"})
        ids = store.list_batch_accounts(platform, batch)
        if not ids:
            return json.dumps({"error": "没有可恢复的账号"})
        from src.proxy.token_rotator import token_rotator
        restored = 0
        failed = 0
        for aid in ids:
            try:
                if store.restore_account(platform, aid):
                    restored += 1
                    token_rotator.clear_disabled(aid)
                else:
                    failed += 1
            except Exception:
                failed += 1
        if restored == 0:
            return json.dumps({"error": "没有可恢复的账号"})
        try:
            token_rotator.reload(platform)
        except Exception:
            pass
        return json.dumps({"success": True, "restored": restored, "failed": failed})

    def destroy_batch(self, platform: str, batch: str) -> str:
        """回收站按批次彻底删除：一次删除的一整组账号物理删除。"""
        try:
            batch = int(batch)
        except (ValueError, TypeError):
            return json.dumps({"error": "无效的批次号"})
        destroyed = store.destroy_batch(platform, batch)
        if destroyed == 0:
            return json.dumps({"error": "没有可删除的账号"})
        try:
            from src.proxy.token_rotator import token_rotator
            token_rotator.reload(platform)
        except Exception:
            pass
        return json.dumps({"success": True, "destroyed": destroyed})

    def set_batch_note(self, platform: str, batch: str, note: str = "") -> str:
        """回收站批次备注：编辑/清空某删除批次的备注（该批次所有账号同步）。"""
        try:
            batch = int(batch)
        except (ValueError, TypeError):
            return json.dumps({"error": "无效的批次号"})
        store.set_batch_note(platform, batch, (note or "").strip())
        return json.dumps({"success": True})

    def import_from_json(self, platform: str, json_content: str, batch_tag: str = "") -> str:
        content = (json_content or "").strip()
        # 批次备注（可选）：作为标签打给这批导入的每个号，方便导入后按标签筛选这批账号
        batch_tag = (batch_tag or "").strip()

        # API Key 格式导入：手机号----ck_xxx（每行一个）。任一行命中即走本分支，
        # 不命中的行跳过。key 是长期凭证（可直接当 Bearer 请求/查额度，无刷新），
        # 存 access_token + refresh_token=None：刷新流程对无 refresh_token 的账号
        # 自动跳过换 token，只拉额度，转发/验活/调度与 OAuth 账号完全一致。
        key_entries, skipped_lines = _parse_apikey_lines(content)
        if key_entries:
            return self._import_apikey_accounts(platform, key_entries, skipped_lines, batch_tag)

        raw = None
        try:
            raw = json.loads(content)
        except json.JSONDecodeError:
            # 不是合法 JSON — 当作裸 access_token 处理。判定收紧：只认单行、
            # 仅含凭证字符集（JWT 的 ._-= / ck_ 密钥 / 手机号+token 的 +）的紧凑
            # 字符串。多行文本、含空格/中文的内容（拖错的诊断日志、说明文件等）
            # 不再被当成超长 token 建垃圾账号，直接报格式错误。
            import re
            if content and re.fullmatch(r"[A-Za-z0-9_\-+\.=]{8,}", content):
                raw = {"access_token": content}
            else:
                return json.dumps({"error": "无法解析：不是有效的账号数据（支持 JSON、手机号----密钥、纯密钥、Token，每行一个）"})

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
                if batch_tag:
                    tags = list(account.tags or [])
                    if batch_tag not in tags:
                        tags.append(batch_tag)
                    account.tags = tags
                # 显式导入 = 恢复意图：清硬删 tombstone + 软删号回原状态，
                # 并清调度器残留冷却/封禁计数
                try:
                    if store.revive_account(platform, account.id):
                        from src.proxy.token_rotator import token_rotator
                        token_rotator.clear_disabled(account.id)
                except Exception:
                    pass
                saved = store.upsert_account(platform, account)
                # 指纹是独立列，不走 upsert；导入时若有指纹需单独落库，否则导出→导入会丢指纹。
                if account.fingerprint:
                    store.save_fingerprint(platform, saved.id, account.fingerprint)
                # 导入即刷新额度：复用 refresh_token 的完整逻辑（拉 dosage/payment/
                # userResource）。否则裸 token 走 build_payload_from_token 拉额度可能
                # 失败/不全，导致额度条不显示，用户还要手动点一次刷新。
                # refresh_token 失败不影响账号已导入，只是那个号额度暂缺。
                try:
                    refreshed = json.loads(self.refresh_token(platform, saved.id))
                    results.append(refreshed if "error" not in refreshed else saved.to_dict())
                except Exception:
                    results.append(saved.to_dict())
            except Exception as e:
                return json.dumps({"error": f"第 {idx + 1} 条解析失败: {e}"})

        # 新账号已落库，同步调度器内存池，否则要等下次 proxy_start/refresh 才进池。
        try:
            from src.proxy.token_rotator import token_rotator
            token_rotator.reload(platform, calibrate=True)
        except Exception:
            pass
        return json.dumps({"success": True, "accounts": results})

    def _import_apikey_accounts(self, platform: str, entries: list, skipped: int,
                                batch_tag: str = "") -> str:
        """API Key 账号导入：手机号优先做账号身份，key 是凭证。

        去重三级（任一命中即复用已有账号，不建新号）：
        1. key 命中   —— 同一把 key 无论行首什么手机号，都是同一账号；
        2. 手机号命中 —— 同一手机号换新 key = 更新凭证（仅 key 账号范围，
           不吞并同手机号的 OAuth 登录号）；
        3. 新账号     —— 唯一 id：有手机号用手机号生成（好对账），纯 key
           用 key 生成，显示名从额度接口反查腾讯 Uin 补。
        与 OAuth 导入共用导入后动作（revive → upsert → 刷额度 → reload 池）；
        refresh_token=None 使刷新流程自动跳过换 token、只拉额度。
        """
        results = []
        for phone, key in entries:
            try:
                account_id = (
                    store.find_account_by_token(platform, key)
                    or (store.find_apikey_by_phone(platform, phone) if phone else None)
                )
                new_account = account_id is None
                if new_account:
                    account_id = Account.generate_id(phone or key)
                # 批次标签：重导入已有号时合并原标签，不覆盖；新号直接打上
                tags = None
                if batch_tag:
                    existing = None if new_account else store.load_account(platform, account_id)
                    tags = list((existing.tags if existing else None) or [])
                    if batch_tag not in tags:
                        tags.append(batch_tag)
                account = Account(
                    id=account_id,
                    email=phone,
                    nickname=phone,
                    access_token=key,
                    refresh_token=None,
                    auth_type="apikey",
                    status="normal",
                    created_at=Account.now_ts(),
                    tags=tags,
                )
                try:
                    if store.revive_account(platform, account.id):
                        from src.proxy.token_rotator import token_rotator
                        token_rotator.clear_disabled(account.id)
                except Exception:
                    pass
                saved = store.upsert_account(platform, account)
                try:
                    refreshed = json.loads(self.refresh_token(platform, saved.id))
                    if isinstance(refreshed, dict) and "error" not in refreshed:
                        # 纯 key 无手机号：用额度接口返回的腾讯账号 Uin 补显示名
                        # （uid/email 空值不会覆盖已有值，upsert COALESCE 保护）
                        if not phone:
                            uin = self._apikey_uin(refreshed)
                            if uin:
                                saved.nickname = uin
                                saved.email = uin
                                saved = store.upsert_account(platform, saved)
                        results.append(refreshed)
                    else:
                        results.append(saved.to_dict())
                except Exception:
                    results.append(saved.to_dict())
            except Exception as e:
                return json.dumps({"error": f"账号 {phone or key[:12]} 导入失败: {e}"})

        try:
            from src.proxy.token_rotator import token_rotator
            token_rotator.reload(platform, calibrate=True)
        except Exception:
            pass
        resp = {"success": True, "accounts": results}
        if skipped:
            resp["skipped"] = skipped
        return json.dumps(resp)

    @staticmethod
    def _apikey_uin(account_dict: dict) -> Optional[str]:
        """从刷完额度的账号数据里提取腾讯账号唯一标识 Uin 当显示名。

        user_resource 的 Accounts[] 每项带 Uin（腾讯账号数字 ID，全账号唯一、
        永不变），取第一个非空值。身份接口（手机号）对 key 不开放，这是纯 key
        导入唯一能反查到的账号唯一信息。
        """
        try:
            accounts = (account_dict.get("quota_raw") or {}).get("userResource") \
                .get("data", {}).get("Response", {}).get("Data", {}).get("Accounts") or []
            for item in accounts:
                uin = str(item.get("Uin") or "").strip()
                if uin:
                    return uin
        except Exception:
            pass
        return None

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

        # 指纹：导出时带 fingerprint，导入时原样还原（按白名单过滤，走 save_fingerprint 独立落库）
        fingerprint = data.get("fingerprint")
        if fingerprint:
            from src.proxy.api_client import FINGERPRINT_FIELDS
            fingerprint = {k: str(v) for k, v in fingerprint.items() if k in FINGERPRINT_FIELDS and v}
            fingerprint = fingerprint or None
        # 账号类型：oauth / apikey。缺失时由调用方根据导入格式推断（key 格式走 apikey）。
        auth_type = data.get("auth_type") or data.get("authType")

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
            auth_type=auth_type or "oauth",
            fingerprint=fingerprint,
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

    def generate_fingerprint(self, style: str = "workbuddy") -> str:
        from src.proxy.api_client import generate_fingerprint
        return json.dumps(generate_fingerprint(style))

    def save_fingerprint(self, platform: str, account_id: str, fingerprint_json: str) -> str:
        fingerprint = json.loads(fingerprint_json) if fingerprint_json else None
        account = store.load_account(platform, account_id)
        if not account:
            return json.dumps({"error": "账号不存在"})
        if fingerprint is not None:
            from src.proxy.api_client import FINGERPRINT_FIELDS
            fingerprint = {k: str(v) for k, v in fingerprint.items() if k in FINGERPRINT_FIELDS and v}
        store.save_fingerprint(platform, account_id, fingerprint)
        account.fingerprint = fingerprint
        # 池内 Account 是缓存对象，不重载的话请求仍用旧指纹 —— reload 让修改即时生效
        from src.proxy.token_rotator import token_rotator
        token_rotator.reload(platform)
        return json.dumps(account.to_dict())

    def batch_generate_fingerprints(self, ids_json: str, style: str = "workbuddy") -> str:
        """批量生成指纹（不落库），返回 [{id, fingerprint}] 供前端预览/修改。

        style: workbuddy（默认，建议）| cli | mixed（每个账号随机一种风格）"""
        import random
        ids = json.loads(ids_json) if ids_json else []
        if not ids:
            return json.dumps({"error": "未选择账号"})
        from src.proxy.api_client import generate_fingerprint
        items = []
        for aid in ids:
            st = style
            if st == "mixed":
                st = random.choice(("workbuddy", "cli"))
            items.append({"id": aid, "fingerprint": generate_fingerprint(st)})
        return json.dumps({"items": items}, ensure_ascii=False)

    def batch_save_fingerprints(self, platform: str, items_json: str) -> str:
        """批量保存指纹（前端预览确认后调用）。

        items=[{id, fingerprint}]，按白名单过滤后落库，末尾 reload 让指纹即时生效。"""
        items = json.loads(items_json) if items_json else []
        if not items:
            return json.dumps({"error": "无数据"})
        saved = 0
        from src.proxy.api_client import FINGERPRINT_FIELDS
        for it in items:
            aid = it.get("id")
            fp = it.get("fingerprint") or {}
            fp = {k: str(v) for k, v in fp.items() if k in FINGERPRINT_FIELDS and v}
            store.save_fingerprint(platform, aid, fp)
            saved += 1
        from src.proxy.token_rotator import token_rotator
        token_rotator.reload(platform)
        return json.dumps({"ok": True, "saved": saved})

    # ========== OAuth Login ==========

    def oauth_start(self, platform: str) -> str:
        # 每次开始新登录，彻底重置 OAuth 状态机：
        # 清掉所有残留的 _pending_oauth（上一次登录成功/失败/取消都可能留下脏条目），
        # 复位 _current_oauth_login_id。这样「重复登录同号」「删了再登录」都从干净状态开始。
        try:
            oauth_api.reset_pending()
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
        is_update = bool(dup_id)   # 已存在 = 覆盖更新；前端据此提示「已更新」而非「新增」
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

        # 显式登录 = 恢复意图：清硬删 tombstone + 软删号回原状态，
        # 并清调度器残留冷却/封禁计数
        try:
            if store.revive_account(platform, account_id):
                from src.proxy.token_rotator import token_rotator
                token_rotator.clear_disabled(account_id)
        except Exception:
            pass
        saved = store.upsert_account(platform, account)
        # 登录即刷新额度：复用 refresh_token 的完整逻辑拉 dosage/payment/userResource。        # 否则新登录的号额度条为空，用户还得手动点刷新（与 import_from_json 保持一致）。
        # 失败不影响账号已保存，只是额度暂缺。
        try:
            refreshed = json.loads(self.refresh_token(platform, saved.id))
            if "error" not in refreshed:
                saved = store.load_account(platform, saved.id) or saved
        except Exception:
            pass
        # OAuth 新增账号，同步调度器内存池让它立即可被调度。
        try:
            from src.proxy.token_rotator import token_rotator
            token_rotator.reload(platform, calibrate=True)
        except Exception:
            pass
        # 登录完成后彻底重置 OAuth 状态机：清掉所有 pending + 复位 _current_oauth_login_id。
        # 否则下次登录时，残留的 pending 条目 / _current_oauth_login_id 会和新流程串味，
        # 触发「没有待处理的登录」（poll_token 找不到 pending 就抛这个错）。
        # 这是「重启网关才好」的根因——重启清空进程内存，但单次登录后内存没清。
        oauth_api.reset_pending()
        self._current_oauth_login_id = None
        # 返回带上 is_update 标志：前端据此提示「账号已更新」而非「登录成功」（重复登录同号时）
        result = saved.to_dict()
        result["is_update"] = is_update
        return json.dumps(result)

    def export_accounts(self, platform: str) -> str:
        """弹原生保存对话框让用户选路径，写入后回报真实落盘位置。

        原先是把 JSON 抛给前端，用 Blob + <a download> 触发下载 —— 在 WebView2 里
        会静默落到系统下载目录，用户无从得知文件在哪，只看到一句「导出成功」。
        """
        accounts = store.list_accounts(platform)
        data = [self._account_export_dict(a) for a in accounts]
        return self._save_accounts_json(platform, data, "accounts")

    def export_selected_accounts(self, platform: str, ids_json: str) -> str:
        """批量导出：只导出用户勾选的账号。与 export_accounts 共用落盘流程。"""
        try:
            ids = set(json.loads(ids_json or "[]"))
        except (json.JSONDecodeError, TypeError):
            return json.dumps({"error": "参数格式错误"})
        if not ids:
            return json.dumps({"error": "没有选中账号"})
        accounts = store.list_accounts(platform)
        data = [self._account_export_dict(a) for a in accounts if a.id in ids]
        if not data:
            return json.dumps({"error": "选中的账号都不存在"})
        return self._save_accounts_json(platform, data, "accounts_selected")

    @staticmethod
    def _account_export_dict(a):
        """账号 → 导出字典。全量导出和选中导出共用，保证字段一致。

        导出完整字段（指纹/auth_type/tags/额度），保证「导出 → 重新导入」闭环
        不丢信息：指纹是独立列、auth_type 区分 key/登录号，漏掉重导就失真。
        """
        return {
            "id": a.id, "email": a.email, "uid": a.uid,
            "nickname": a.nickname, "enterprise_id": a.enterprise_id,
            "enterprise_name": a.enterprise_name,
            "access_token": a.access_token, "refresh_token": a.refresh_token,
            "token_type": a.token_type, "expires_at": a.expires_at,
            "domain": a.domain, "status": a.status,
            "auth_type": a.auth_type,
            "fingerprint": a.fingerprint,
            "tags": a.tags,
            "quota_raw": a.quota_raw,
            "usage_raw": a.usage_raw,
            "plan_type": a.plan_type,
            "payment_type": a.payment_type,
            "last_checkin_time": a.last_checkin_time,
            "checkin_streak": a.checkin_streak,
        }

    def _save_accounts_json(self, platform: str, data: list, name_prefix: str) -> str:
        """共享：弹保存对话框 + 写盘。返回给前端的 JSON 结果。"""
        if not data:
            return json.dumps({"error": "当前没有账号可导出"})
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        if not self._window:
            return json.dumps({"error": "窗口未就绪"})
        default_name = f"{name_prefix}_{platform}_{time.strftime('%Y-%m-%d')}.json"
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
        try:
            v = float(value)
        except (ValueError, TypeError):
            return json.dumps({"error": f"无效的阈值: {value}"})
        if v < 0:
            v = 0
        token_rotator.set_threshold(v)
        return json.dumps({"ok": True, "threshold": v})

    def get_enable_threshold(self) -> str:
        from src.proxy.token_rotator import token_rotator
        return json.dumps({"threshold": token_rotator.get_enable_threshold()})

    def set_enable_threshold(self, value: float) -> str:
        from src.proxy.token_rotator import token_rotator
        try:
            v = float(value)
        except (ValueError, TypeError):
            return json.dumps({"error": f"无效的阈值: {value}"})
        if v < 0:
            v = 0
        token_rotator.set_enable_threshold(v)
        return json.dumps({"ok": True, "threshold": v})

    def get_all_stats(self, platform: str) -> str:
        from src.storage.store import list_account_stats
        stats = list_account_stats(platform)
        return json.dumps({"stats": stats})

    def get_usage_chart_data(self, platform: str, days: int = 30) -> str:
        from src.storage.store import get_usage_summary
        summary = get_usage_summary(platform)
        return json.dumps({"daily": [], "summary": summary})

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
        total_remain = 0
        # 叠加本次周期实际消耗（account_stats.total_credit），让顶部额度条反映
        # 网关运行期的实时扣减，而不是 quota_raw 的陈旧快照。与卡片 getCardQuota 同口径。
        stats_map = {}
        try:
            for s in store.list_account_stats(platform):
                stats_map[s.get("account_id")] = s
        except Exception:
            pass
        for a in accounts:
            if a.status == "banned":
                continue
            try:
                t, u = quota_api.calc_totals(a.quota_raw, a.usage_raw, active_only=False)
            except Exception:
                t, u = 0, 0
            # 本次周期消耗积分（刷新额度时会被 reset_account_credit 清零）
            credit = float((stats_map.get(a.id) or {}).get("total_credit") or 0)
            used = u + credit
            total_quota += t
            total_used += used
            total_remain += max(0.0, t - used)
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
            "total_remain": total_remain,
            "checked_in_today": checked_in,
        })

    # ========== Token Operations ==========

    def _persist_refreshed(self, platform: str, account: Account, payload: dict,
                           quota_error: Optional[str], force_normal: bool = False) -> Account:
        """把 refresh_full_payload 的结果写回账号并落库，返回落库后的账号。

        force_normal=True 时，只要未被明确封禁就把状态置为 normal —— 用于
        「验活并启用」：达标的禁用/封禁号需要重新启用。force_normal=False 时
        保持与原 refresh 完全一致的状态策略（不动手动禁用/封禁的号）。
        """
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
            if new_status == "banned":
                account.status = "banned"
            elif force_normal:
                account.status = "normal"
            elif account.status not in ("disabled", "banned"):
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
            store.reset_account_credit(platform, account.id)
        except Exception:
            pass
        return saved

    def refresh_token(self, platform: str, account_id: str, _reload: bool = True) -> str:
        account = store.load_account(platform, account_id)
        if not account:
            return json.dumps({"error": "账号不存在"})

        try:
            payload, quota_error = refresh_full_payload(account)
            saved = self._persist_refreshed(platform, account, payload, quota_error, force_normal=False)
            if _reload:
                try:
                    from src.proxy.token_rotator import token_rotator
                    token_rotator.reload(platform, calibrate=True)
                except Exception:
                    pass
            return json.dumps(saved.to_dict())
        except Exception as e:
            return json.dumps({"error": str(e)})

    def _detect_one(self, platform: str, account: Account) -> dict:
        """单账号封禁验活核心（按 cbcn-cloud 号池封禁机制）：
        refresh 续期 + 拉额度落库 → 发真实 chat 请求探测 11140：
          全 11140 → 标记 banned（封号）；
          任意 200 → 验活通过，若原是 banned 则恢复 normal；
          其他错误 → unknown，不封不禁。
        只写状态不 reload（reload 由调用方统一做，批量时避免反复重载）。
        """
        from src.proxy.probe import probe_chat_available
        from src.proxy.token_rotator import token_rotator

        name = account.nickname or account.email or account.id
        try:
            payload, quota_error = refresh_full_payload(account)
        except Exception as e:
            return {"id": account.id, "name": name, "status": "failed", "reason": f"刷新失败: {e}"}
        try:
            self._persist_refreshed(platform, account, payload, quota_error, force_normal=False)
        except Exception as e:
            return {"id": account.id, "name": name, "status": "failed", "reason": f"落库失败: {e}"}

        try:
            result = probe_chat_available(account, payload.get("access_token") or account.access_token)
        except Exception as e:
            return {"id": account.id, "name": name, "status": "unknown", "reason": f"探测异常: {e}"}

        # 验活结果留痕（受统一日志开关控制）：ok/banned/unknown + 判定依据
        try:
            from src.storage.store import add_log
            detail = {
                "banned": "真实 chat 请求 3 次均被拒(11140)",
                "ok": "真实 chat 请求成功",
                "unknown": "网络/接口异常，不算封号证据",
            }.get(result, "")
            add_log("upstream", platform, account.id, name, "", f"验活 → {result}", detail)
        except Exception:
            pass

        if result == "banned":
            account.status = "banned"
            store.upsert_account(platform, account)
            # 即时同步调度器内存：否则批量验活要等全部 future 完成（可达数十秒）
            # 才统一 reload，期间网关仍会调度这个已确认封号的号 → 触发更多 11140 风控。
            # 这里直接改内存池里的 Account 对象 + 若是当前号则切走。
            try:
                with token_rotator._lock:
                    for a in token_rotator._accounts:
                        if a.id == account.id:
                            a.status = "banned"
                            break
                    if token_rotator._current_id == account.id:
                        # 设暂存原因，get_next 换号时写日志"验活封号"
                        token_rotator._pending_switch_from = account.id
                        token_rotator._pending_switch_from_nick = name
                        token_rotator._pending_switch_reason = "验活封号"
                        token_rotator._current_id = None
                    token_rotator._disabled.pop(account.id, None)
            except Exception:
                pass
            return {"id": account.id, "name": name, "status": "banned",
                    "reason": "真实请求 3 次均被拒(11140 封号)"}
        if result == "ok":
            # 验活通过。只有原状态是 banned（封禁）才恢复 normal；
            # 手动 disabled 的号保持 disabled —— 验活是检测封号，不能覆盖用户手动禁用。
            if account.status == "banned":
                account.status = "normal"
                store.upsert_account(platform, account)
                token_rotator.clear_disabled(account.id)
                return {"id": account.id, "name": name, "status": "normal",
                        "reason": "验活通过，已从封禁恢复为正常"}
            if account.status == "disabled":
                return {"id": account.id, "name": name, "status": "disabled",
                        "reason": "验活通过，保持手动禁用"}
            # 普通账号验活通过 = 上游真实请求打通 → 顺手清 transient 限流标记
            # （限流是无限期探测制，验活就是最好的手动探测入口）
            try:
                if account.id in token_rotator._disabled:
                    token_rotator.clear_disabled(account.id)
                    return {"id": account.id, "name": name, "status": "normal",
                            "reason": "验活通过，已解除限流"}
            except Exception:
                pass
            return {"id": account.id, "name": name, "status": "normal", "reason": "验活通过"}
        return {"id": account.id, "name": name, "status": "unknown",
                "reason": "非封号错误，未判定（限流/额度/网络等）"}

    def detect_account(self, platform: str, account_id: str) -> str:
        from src.proxy.token_rotator import token_rotator
        account = store.load_account(platform, account_id)
        if not account:
            return json.dumps({"error": "账号不存在"})
        try:
            r = self._detect_one(platform, account)
            token_rotator.reload(platform, calibrate=True)
            return json.dumps({"status": r["status"], "reason": r["reason"]})
        except Exception as e:
            return json.dumps({"error": str(e)})

    def detect_accounts(self, platform: str, account_ids_json: str) -> str:
        """并发批量验活（线程池 8，单账号同款真实 chat 探测），统一 reload。

        返回汇总：{total, counts:{normal,banned,unknown,failed}, results}。
        """
        import concurrent.futures
        # 与阈值验活互斥：避免同一账号被双重探测浪费额度/触发风控（B5）
        started = False
        with self._detect_lock:
            if self._detect_state.get("running"):
                return json.dumps({"error": "阈值验活正在进行中，请稍后再试"})
            self._detect_state["running"] = True
            started = True
        try:
            try:
                ids = [str(i) for i in json.loads(account_ids_json or "[]")]
            except (ValueError, TypeError):
                return json.dumps({"error": "无效的账号列表"})
            if not ids:
                return json.dumps({"error": "请先勾选账号"})

            targets = [a for a in store.list_accounts(platform) if a.id in ids]
            if not targets:
                return json.dumps({"error": "账号不存在"})

            from src.proxy.token_rotator import token_rotator
            results = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
                futs = [ex.submit(self._detect_one, platform, a) for a in targets]
                for fut in concurrent.futures.as_completed(futs):
                    try:
                        results.append(fut.result())
                    except Exception as e:
                        results.append({"id": "?", "name": "?", "status": "failed", "reason": str(e)})
            try:
                token_rotator.reload(platform, calibrate=True)
            except Exception:
                pass

            counts = {"normal": 0, "banned": 0, "unknown": 0, "failed": 0}
            for r in results:
                counts[r["status"]] = counts.get(r["status"], 0) + 1
            return json.dumps({"total": len(results), "counts": counts, "results": results})
        finally:
            with self._detect_lock:
                self._detect_state["running"] = False

    def refresh_all(self, platform: str) -> str:
        accounts = store.list_accounts(platform)
        success = 0
        # 批量刷新：循环内不 reload（避免 N 个账号触发 N 次持锁重建池阻塞网关），
        # 全部刷完统一 reload 一次。
        for acc in accounts:
            result = json.loads(self.refresh_token(platform, acc.id, _reload=False))
            if "error" not in result:
                success += 1
        try:
            from src.proxy.token_rotator import token_rotator
            token_rotator.reload(platform, calibrate=True)
        except Exception:
            pass
        return json.dumps({"success": success, "total": len(accounts)})

    def detect_and_enable_accounts(self, platform: str, threshold: float = -1) -> str:
        """并发验活全部账号：拉取最新额度，达标的禁用号自动启用。

        threshold<0 时用已持久化的启动阈值。后台线程池跑（8 并发），
        前端通过 detect_enable_status 轮询进度。

        判定：
          - 封禁（refresh 判 banned 或原状态 banned）→ 跳过，保持封禁（恢复需真实 chat 验活）；
          - normal 账号 → 保持启用，刷新额度数据；
          - disabled 账号剩余(total-used) >= 阈值（阈值=0 时不卡门槛）→ 启用；
          - 额度拉取失败但非封禁（超时/网络等）→ 视为无法判定但可能可用 → 仍启用。
        """
        import concurrent.futures
        from src.api.quota import calc_totals
        from src.proxy.token_rotator import token_rotator

        try:
            th = float(threshold)
        except (ValueError, TypeError):
            th = -1.0
        if th < 0:
            th = token_rotator.get_enable_threshold()

        with self._detect_lock:
            if self._detect_state.get("running"):
                return json.dumps({"error": "验活正在进行中"})
            accounts = store.list_accounts(platform)
            targets = list(accounts)
            self._detect_state = {
                "running": True, "finished": False,
                "total": len(targets), "done": 0,
                "enabled": 0, "skipped": 0, "banned": 0, "checked": 0, "failed": 0,
                "last_account": "", "summary": "",
            }

        if not targets:
            with self._detect_lock:
                self._detect_state["running"] = False
                self._detect_state["finished"] = True
                self._detect_state["summary"] = "没有账号可验活"
            return json.dumps({"ok": True, "started": False, "message": "没有账号可验活"})

        def worker(acc):
            name = acc.nickname or acc.email or acc.id
            was = acc.status or "normal"
            try:
                payload, quota_error = refresh_full_payload(acc)
            except Exception as e:
                return ("failed", name, f"验活异常: {e}")
            if payload.get("status") == "banned" or was == "banned":
                return ("banned", name, "账号已封禁，跳过（恢复需真实 chat 验活）")
            if was == "normal":
                try:
                    self._persist_refreshed(platform, acc, payload, quota_error, force_normal=False)
                except Exception:
                    pass
                return ("checked", name, "正常，已刷新额度")
            remain = None
            if quota_error is None:
                try:
                    total, used = calc_totals(payload.get("quota_raw"), payload.get("usage_raw"))
                except Exception:
                    total, used = 0.0, 0.0
                remain = max(0.0, float(total) - float(used))
                if th > 0 and remain < th:
                    return ("skipped", name, f"剩余{remain:.2f}<{th}，未启用")
            try:
                self._persist_refreshed(platform, acc, payload, quota_error, force_normal=True)
            except Exception as e:
                return ("failed", name, f"启用落库失败: {e}")
            try:
                token_rotator.clear_disabled(acc.id)
            except Exception:
                pass
            if remain is None:
                return ("enabled", name, "额度拉取失败但未封禁，已启用")
            return ("enabled", name, f"剩余{remain:.2f}≥{th}，已启用")

        def run():
            counts = {"enabled": 0, "skipped": 0, "banned": 0, "checked": 0, "failed": 0}
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
                    futs = [ex.submit(worker, a) for a in targets]
                    for fut in concurrent.futures.as_completed(futs):
                        try:
                            kind, name, _msg = fut.result()
                        except Exception:
                            kind, name = "failed", "?"
                        with self._detect_lock:
                            counts[kind] = counts.get(kind, 0) + 1
                            self._detect_state["done"] += 1
                            self._detect_state[kind] = counts[kind]
                            self._detect_state["last_account"] = name
                try:
                    token_rotator.reload(platform, calibrate=True)
                except Exception:
                    pass
            finally:
                with self._detect_lock:
                    self._detect_state["running"] = False
                    self._detect_state["finished"] = True
                    self._detect_state["summary"] = (
                        f"启用 {counts['enabled']} / 跳过 {counts['skipped']} / "
                        f"封禁 {counts['banned']} / 正常 {counts['checked']} / "
                        f"失败 {counts['failed']}"
                    )

        threading.Thread(target=run, daemon=True).start()
        return json.dumps({"ok": True, "started": True, "total": len(targets)})

    def detect_enable_status(self) -> str:
        with self._detect_lock:
            return json.dumps(self._detect_state)

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

    def get_machine_code(self) -> str:
        """返回当前机器 ID（激活界面展示用）。"""
        try:
            from src import license as lic
            return lic.machine_code()
        except Exception:
            return ""

    def check_license(self) -> str:
        enabled = _LICENSE_ENABLED if _LICENSE_ENABLED is not None else _resolve_license_enabled()
        if not enabled:
            # 免授权模式：不走 verify，但 config 公告仍要透传（启动即送达）
            result = {"licensed": True, "expiry": None, "message": "OK"}
            if _CONFIG_ANNOUNCEMENT:
                result["announcement"] = _CONFIG_ANNOUNCEMENT
            return json.dumps(result, ensure_ascii=False)
        from src import license as lic
        st = lic.status()
        if st["expiry"]:
            st["expiry_str"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(st["expiry"]))
        # config 公告兜底：verify 可能因版本被拦/未激活/断网而拿不到公告，
        # 但 config 是启动第一跳，公告已缓存 —— 这里补上，保证启动即弹。
        if _CONFIG_ANNOUNCEMENT and "announcement" not in st:
            st["announcement"] = _CONFIG_ANNOUNCEMENT
        if st.get("licensed"):
            start_license_heartbeat(self._window)  # 心跳：在线追踪 + 运行途中吊销即时生效
        else:
            # 版本被拦（服务端 min_version / 黑名单）→ 前端显示升级提示而非激活输入框
            msg = st.get("message") or ""
            if "版本" in msg and "升级" in msg:
                st["version_blocked"] = True
        return json.dumps(st, ensure_ascii=False)

    def activate(self, code: str) -> str:
        from src import license as lic
        ok, exp, msg = lic.activate(code)
        if ok:
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

        if _LICENSE_ENABLED if _LICENSE_ENABLED is not None else _resolve_license_enabled():
            from src import license as lic
            st = lic.status()
            if not st.get("licensed"):
                return json.dumps({"error": st.get("message") or "授权无效，请先激活"})

        existing = json.loads(self.proxy_status())
        if existing.get("running"):
            return json.dumps({"error": "网关已在运行"})

        port_num = int(port) if port.strip() else 8001
        # 监听地址：默认 0.0.0.0（局域网可访问）；可用 CBCN_PROXY_HOST 覆盖为指定 IP
        bind_host = (os.environ.get("CBCN_PROXY_HOST") or "0.0.0.0").strip() or "0.0.0.0"

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            try:
                sock.bind((bind_host, port_num))
            except OSError:
                return json.dumps({"error": f"端口 {port_num} 已被占用，请换一个端口"})
        finally:
            sock.close()

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
                host=bind_host,
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
            # 集成 WorkBuddy / ZCode：注入快捷方式 CDP 参数（用户双击桌面图标即带调试端口），
            # 后台轮询检测到 CDP 起来后自动注入额度横条（应用未开则静默等）
            try:
                from src.gui.wb_shortcut import inject as _wb_shortcut_inject
                _wb_shortcut_inject("workbuddy")
                _wb_shortcut_inject("zcode")
            except Exception:
                pass
            self._start_cdp_inject_loop(port_num)
            # key 持久化：下次启动自动回填，避免每次重输
            try:
                store.save_setting("proxy_key", password)
            except Exception:
                pass
            return json.dumps({"success": True, "port": port_num, "lan_ips": _lan_ips()})
        except Exception as e:
            return json.dumps({"error": f"网关启动失败: {str(e)[:200]}"})

    def _start_cdp_inject_loop(self, port_num: int):
        """后台线程：WorkBuddy（9222）/ ZCode（9223）CDP 起来且横条不在时注入额度横条。
        应用重启 / 页面 reload 后横条丢失会自动重注入。
        退出条件：捕获启动时的 server 引用——代理停止（_proxy_server 置 None）或
        已换新 server（快速 stop→start）时线程自然退出，杜绝死线程/双线程并存。"""

        server_ref = getattr(self, "_proxy_server", None)
        if server_ref is None:
            return

        def _loop():
            import time
            from src.gui.cdp_injector import inject_quota_bar, bar_present, INJECT_TARGETS
            while getattr(self, "_proxy_server", None) is server_ref:
                for cdp_port in INJECT_TARGETS:
                    try:
                        if not bar_present(cdp_port):
                            inject_quota_bar(port_num, cdp_port)
                    except Exception:
                        pass
                time.sleep(5)

        threading.Thread(target=_loop, daemon=True).start()

    def proxy_stop(self) -> str:
        # 注意：停代理不还原 WorkBuddy 快捷方式 CDP 参数——
        # 只有程序真正关闭（cleanup）时才还原，避免停代理后 WorkBuddy 失去 CDP/横条
        server = getattr(self, "_proxy_server", None)
        if server:
            server.should_exit = True
            self._proxy_server = None
            # 丢弃旧 httpx client 引用：它绑定在即将关闭的旧事件循环上，
            # 重启网关后 _get_http_client 会按新 loop 重建。这里只置 None，
            # 不 await aclose()——GUI 线程拿不到旧 loop，await 会抛 Event loop is closed。
            try:
                from src.proxy import proxy_server as _ps
                _ps._http_client = None
                _ps._http_client_loop = None
            except Exception:
                pass
            try:
                from src.proxy.token_rotator import token_rotator
                token_rotator._active_count = 0
                token_rotator.persist_estimates()
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
            "lan_ips": _lan_ips(),
            "proxy_key": store.get_setting("proxy_key", ""),
        })

    def cleanup(self, reason="atexit"):
        # 退出标记：写进 runtime.log。下次"网关自动关闭"时靠它区分退出方式——
        # win_close=用户点了退出/托盘退出/更新重启；atexit=系统关闭路径(Alt+F4、
        # 任务栏关闭、Windows 注销)；两条都没有=进程被强杀或 native 崩溃(看 crash.log)。
        try:
            from src.gui.log_setup import write_runtime_log
            write_runtime_log(f"进程退出（{reason}）", "INFO")
        except Exception:
            pass
        # 兜底还原 WorkBuddy 快捷方式 CDP 参数（异常退出也还原）
        try:
            from src.gui.wb_shortcut import restore as _wb_shortcut_restore
            _wb_shortcut_restore()
        except Exception:
            pass
        server = getattr(self, "_proxy_server", None)
        if server:
            server.should_exit = True
            self._proxy_server = None
        # 与 proxy_stop 对齐：归零 active_count，否则重启网关时边框动画状态会错乱。
        # 持久化内存估算值，重启后恢复，防止丢扣减记录。
        try:
            from src.proxy.token_rotator import token_rotator
            token_rotator._active_count = 0
            token_rotator.persist_estimates()
        except Exception:
            pass

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
        except Exception as e:
            return _json.dumps({"ok": False, "error": str(e)})
        return _json.dumps({"ok": True})

    def set_account_statuses(self, platform: str, account_ids_json: str, status: str) -> str:
        """批量设置账号状态（normal / disabled）。
        禁用时保护：不能把池中所有可用账号都禁掉（同删除的最后号保护）。"""
        import json as _json
        try:
            ids = [str(i) for i in _json.loads(account_ids_json or "[]")]
        except (ValueError, TypeError):
            return _json.dumps({"error": "无效的账号列表"})
        if not ids:
            return _json.dumps({"error": "没有选中账号"})
        from src.proxy.token_rotator import token_rotator
        if status == "disabled":
            try:
                token_rotator.ensure_loaded(platform)
                if not token_rotator.has_usable_besides(ids):
                    return _json.dumps({"error": "不能禁用全部可用账号（至少保留一个可用账号）"})
            except Exception:
                pass
        done = 0
        failed = 0
        for aid in ids:
            try:
                acc = store.load_account(platform, aid)
                if not acc:
                    failed += 1
                    continue
                if status == "disabled" and acc.status in ("disabled", "banned"):
                    continue  # 已是禁用/封禁，跳过
                if status == "normal" and acc.status == "normal":
                    continue  # 已正常，跳过
                if status == "disabled":
                    # 走 on_disable：若禁用的是当前调度号，会立即切换并写切号日志（有原因），
                    # 否则 reload 时 current 失效才被动切号，日志缺原因。
                    token_rotator.on_disable(aid)
                acc.status = status
                store.upsert_account(platform, acc)
                if status == "normal":
                    token_rotator.clear_disabled(aid)
                done += 1
            except Exception:
                failed += 1
        try:
            token_rotator.reload(platform)
        except Exception:
            pass
        return _json.dumps({"ok": True, "done": done, "failed": failed, "total": len(ids)})

    def refresh_accounts(self, platform: str, account_ids_json: str) -> str:
        """批量刷新选中账号额度（复用单号刷新逻辑）。"""
        import json as _json
        try:
            ids = [str(i) for i in _json.loads(account_ids_json or "[]")]
        except (ValueError, TypeError):
            return _json.dumps({"error": "无效的账号列表"})
        if not ids:
            return _json.dumps({"error": "没有选中账号"})
        success = 0
        failed = 0
        # 批量刷新不逐个 reload（避免 N 次持锁重建池阻塞网关），末尾统一 reload。
        for aid in ids:
            try:
                r = _json.loads(self.refresh_token(platform, aid, _reload=False))
                if "error" not in r:
                    success += 1
                else:
                    failed += 1
            except Exception:
                failed += 1
        try:
            from src.proxy.token_rotator import token_rotator
            token_rotator.reload(platform, calibrate=True)
        except Exception:
            pass
        return _json.dumps({"ok": True, "success": success, "failed": failed, "total": len(ids)})

    def checkin_accounts(self, platform: str, account_ids_json: str) -> str:
        """批量签到选中账号（复用单号签到逻辑）。"""
        import json as _json
        try:
            ids = [str(i) for i in _json.loads(account_ids_json or "[]")]
        except (ValueError, TypeError):
            return _json.dumps({"error": "无效的账号列表"})
        if not ids:
            return _json.dumps({"error": "没有选中账号"})
        success = 0
        already = 0
        failed = 0
        for aid in ids:
            try:
                r = _json.loads(self.checkin(platform, aid))
                if r.get("error"):
                    failed += 1
                elif r.get("success"):
                    success += 1
                else:
                    msg = (r.get("message") or "").lower()
                    if "already" in msg or "checked" in msg:
                        already += 1
                    else:
                        failed += 1
            except Exception:
                failed += 1
        return _json.dumps({"ok": True, "success": success, "already": already, "failed": failed, "total": len(ids)})

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

    def _build_model_configs(self, port_num: int, api_key: str) -> list:
        from src.proxy.api_client import MODEL_SPECS
        config = []
        for mid, spec in MODEL_SPECS.items():
            entry = {
                "id": spec["name"],
                "name": spec["name"],
                "vendor": "Gateway",
                "url": f"http://localhost:{port_num}/v1",
                "apiKey": api_key,
                "supportsToolCall": True,
                "supportsImages": True,
                "supportsReasoning": spec["reasoning"],
                "useCustomProtocol": False,
                "maxInputTokens": spec["context"],
                "maxOutputTokens": spec["output"],
            }
            if spec["reasoning"]:
                entry["reasoning"] = {
                    "supportedEfforts": ["low", "medium", "high", "xhigh"],
                    "defaultEffort": "high",
                }
            config.append(entry)
        return config

    @staticmethod
    def _write_config_file(target, payload) -> dict:
        import pathlib
        backup = None
        if target.exists():
            backup = target.with_suffix(".json.bak")
            try:
                backup.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
            except Exception:
                backup = None
        target.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return {"success": True, "path": str(target),
                "count": len(payload) if isinstance(payload, list) else len(payload.get("models", [])),
                "backup": str(backup) if backup else ""}

    def export_config(self, target: str, port: str = "", password: str = "") -> str:
        """导出网关配置到本地 IDE 的配置文件。
        target='workbuddy' → ~/.workbuddy/models.json（裸数组）
        target='codebuddy' → ~/.codebuddy/models.json（{"models": [...]} 包裹）
        target='zcode'     → ~/.zcode/v2/config.json（合并进 provider 字典，不动用户其他配置）
        """
        import pathlib
        port_num = int(port) if port and port.strip() else getattr(self, "_proxy_port", 8001)
        api_key = password.strip() if password else ""
        if target == "zcode":
            return json.dumps(self._export_zcode_config(port_num, api_key))
        config = self._build_model_configs(port_num, api_key)
        profiles = {
            "workbuddy": {"folder": ".workbuddy", "app": "WorkBuddy", "wrap": config},
            "codebuddy": {"folder": ".codebuddy", "app": "CodeBuddy", "wrap": {"models": config}},
        }
        prof = profiles.get(target, profiles["workbuddy"])
        candidates = [
            pathlib.Path.home() / prof["folder"] / "models.json",
            pathlib.Path(os.environ.get("APPDATA", "")) / prof["app"] / prof["folder"] / "models.json",
        ]
        tgt = None
        for p in candidates:
            if p.parent.exists():
                tgt = p
                break
        if not tgt:
            tgt = candidates[0]
            tgt.parent.mkdir(parents=True, exist_ok=True)
        try:
            return json.dumps(self._write_config_file(tgt, prof["wrap"]))
        except Exception as e:
            return json.dumps({"error": str(e)})

    # ZCode 网关 provider 的固定 UUID：重复导入时 upsert 到同一 provider，不产生重复条目
    _ZCODE_PROVIDER_ID = "a1b2c3d4-0000-4000-8000-cbcn2apigw01"

    def _export_zcode_config(self, port_num: int, api_key: str) -> dict:
        """把网关作为一个 openai-compatible provider 合并进 ~/.zcode/v2/config.json。
        读现有配置 → upsert provider → 写回（先备份）。模型列表按 ZCode 的
        config 结构生成（limit/modalities/reasoning），与手写配置同构。"""
        from src.proxy.api_client import MODEL_SPECS
        import pathlib
        tgt = pathlib.Path.home() / ".zcode" / "v2" / "config.json"
        try:
            cfg = json.loads(tgt.read_text(encoding="utf-8")) if tgt.exists() else {}
        except Exception:
            cfg = {}
        providers = cfg.setdefault("provider", {})
        models = {}
        for mid, spec in MODEL_SPECS.items():
            m = {
                "name": spec["name"],
                "limit": {"context": spec["context"], "output": spec["output"]},
                "modalities": {"input": ["text"], "output": ["text"]},
            }
            if spec["reasoning"]:
                m["reasoning"] = {
                    "enabled": True,
                    "variants": ["off", "high", "max"],
                    "defaultVariant": "max",
                }
            models[mid] = m
        existing = providers.get(self._ZCODE_PROVIDER_ID, {})
        providers[self._ZCODE_PROVIDER_ID] = {
            "name": "AI Gateway",
            "kind": "openai-compatible",
            "options": {
                "apiKey": api_key or "none",
                "baseURL": f"http://127.0.0.1:{port_num}/v1",
                "apiKeyRequired": True,
            },
            "enabled": True,
            "source": "custom",
            # 用户在 ZCode 里改过的模型设置（zcode.modified 等）保留，不粗暴覆盖
            "models": {**models, **{k: v for k, v in (existing.get("models") or {}).items()
                                     if k in models and v.get("zcode", {}).get("modified")}},
        }
        backup = ""
        if tgt.exists():
            bak = tgt.with_suffix(".json.bak-gw")
            try:
                bak.write_text(tgt.read_text(encoding="utf-8"), encoding="utf-8")
                backup = str(bak)
            except Exception:
                pass
        try:
            tgt.parent.mkdir(parents=True, exist_ok=True)
            tgt.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
            return {"success": True, "path": str(tgt), "count": len(models), "backup": backup}
        except Exception as e:
            return {"error": str(e)}

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

    def export_diagnostics(self) -> str:
        """打包诊断信息为 txt：版本/机器码/系统/授权/事件日志/运行日志，弹保存对话框。

        面向「用户报错 → 一键导出 → 开发者排错」闭环。事件日志来自 proxy_logs
        （各平台最近 500 条合并按时间倒序）；运行日志来自 DB_DIR/runtime.log
        （由全局异常捕获写入，若不存在则跳过）。
        """
        import platform as _plat
        from src.updater import APP_VERSION
        lines = []
        lines.append("=" * 60)
        lines.append("AI Gateway 诊断信息")
        lines.append("=" * 60)
        lines.append(f"导出时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"版本: {APP_VERSION}")
        try:
            from src import license as lic
            mc = lic.machine_code()
        except Exception as e:
            mc = f"获取失败: {e!r}"
        lines.append(f"机器码: {mc}")
        lines.append(f"操作系统: {_plat.platform()}")
        lines.append(f"架构: {_plat.machine()}")
        lines.append(f"Python: {_plat.python_version()}")
        lines.append(f"数据目录: {store.DB_DIR}")
        lines.append(f"授权检查: {'需授权' if _LICENSE_ENABLED else '免授权'}")
        lines.append("")
        lines.append("-" * 60)
        lines.append("事件日志（最近 500 条）")
        lines.append("-" * 60)
        try:
            from src.storage.store import list_logs
            all_logs = []
            for p in ("workbuddy", "codebuddy"):
                try:
                    all_logs.extend(list_logs(p, limit=500))
                except Exception:
                    pass
            all_logs.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
            for lg in all_logs[:500]:
                ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(lg.get("timestamp", 0)))
                lines.append(
                    f"[{ts}] {lg.get('platform','')} | {lg.get('event','')} | "
                    f"{lg.get('account_id','')} | {lg.get('detail','')}"
                )
            if not all_logs:
                lines.append("（无事件日志）")
        except Exception as e:
            lines.append(f"事件日志读取失败: {e!r}")
        # 运行日志文件（由全局异常捕获写入，不存在则跳过）
        run_log = store.DB_DIR / "runtime.log"
        if run_log.exists():
            lines.append("")
            lines.append("-" * 60)
            lines.append("运行日志（runtime.log 末尾 300 行）")
            lines.append("-" * 60)
            try:
                tail = run_log.read_text(encoding="utf-8", errors="replace").splitlines()[-300:]
                lines.extend(tail)
            except Exception as e:
                lines.append(f"运行日志读取失败: {e!r}")
        content = "\n".join(lines)
        if not self._window:
            return json.dumps({"error": "窗口未就绪"})
        try:
            import webview
            result = self._window.create_file_dialog(
                webview.FileDialog.SAVE,
                directory=str(Path.home() / "Downloads"),
                save_filename=f"diagnostics-{time.strftime('%Y%m%d-%H%M%S')}.txt",
                file_types=("文本文件 (*.txt)",),
            )
        except Exception as e:
            return json.dumps({"error": f"打开保存对话框失败: {e}"})
        if not result:
            return json.dumps({"cancelled": True})
        path = result if isinstance(result, str) else result[0]
        if not path.lower().endswith(".txt"):
            path += ".txt"
        try:
            Path(path).write_text(content, encoding="utf-8")
        except Exception as e:
            return json.dumps({"error": f"写入失败: {e}"})
        return json.dumps({"ok": True, "path": path})

    def log_js_error(self, message: str, source: str = "", lineno: int = 0,
                     colno: int = 0, stack: str = "") -> str:
        """前端 window.onerror 兜底：把 JS 未捕获异常写入 runtime.log（诊断闭环）。"""
        try:
            from src.gui.log_setup import write_runtime_log
            write_runtime_log(
                f"[JS ERROR] {message}\n"
                f"  at {source}:{lineno}:{colno}\n"
                f"  {stack}"
            )
        except Exception:
            pass
        return json.dumps({"ok": True})

    # ========== Auto Update ==========

    def check_update(self) -> str:
        from src.updater import check_latest
        return json.dumps(check_latest())

    def download_update(self, url: str) -> str:
        from src.updater import download_update
        self._dl_progress = 0
        result = download_update(url, progress_callback=lambda pct: setattr(self, "_dl_progress", pct))
        return json.dumps(result)

    def get_download_progress(self) -> str:
        """前端轮询下载进度（0-100）。"""
        return json.dumps({"pct": getattr(self, "_dl_progress", 0)})

    def apply_update(self, path: str) -> str:
        from src.updater import apply_update
        result = apply_update(path)
        return json.dumps(result)
