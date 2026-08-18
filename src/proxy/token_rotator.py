import json
import logging
import time
import threading
from typing import Optional

from src.storage import store
from src.models.account import Account
from src.api.quota import calc_totals

logger = logging.getLogger(__name__)

# 冷却时长（秒）
QUOTA_COOLDOWN = 3600   # 额度耗尽（429）：1 小时后重试
AUTH_COOLDOWN = 600     # 非法请求（403）：10 分钟后重试
TRANSIENT_COOLDOWN = 60  # 临时错误（401/502/503/504/超时）：1 分钟后重试
BANNED_COOLDOWNS = (30, 60, 120, 300, 600)  # 11140 封号：渐进冷却 30s→1m→2m→5m→10m

_COOLDOWN_SETTING_KEY = "cooldowns"
_THRESHOLD_SETTING_KEY = "quota_threshold"
_ENABLE_THRESHOLD_SETTING_KEY = "enable_threshold"
_ESTIMATE_SETTING_KEY = "estimated_remain"

_COOLDOWNS = {
    "quota": QUOTA_COOLDOWN,
    "auth": AUTH_COOLDOWN,
    "transient": TRANSIENT_COOLDOWN,
}


def _snapshot_usable(quota_raw: Optional[dict], usage_raw: Optional[dict]) -> bool:
    """快照形状可用：能解析出至少一个套餐包的额度字段（total/remain 任一可转数字）。

    与 calc_totals 取数路径一致（quota_raw.userResource → usage_raw 兜底）。
    快照不可用的账号（接口异常/字段缺失）估算不可信：
    不覆盖内存估算、不触发阈值切号，真实额度耗尽交给上游 429 兜底。
    """
    ur = (quota_raw or {}).get("userResource") if quota_raw else None
    if not ur:
        ur = usage_raw
    if not ur or not isinstance(ur, dict):
        return False
    try:
        accounts = (ur.get("data") or {}).get("Response", {}).get("Data", {}).get("Accounts") or []
    except AttributeError:
        return False
    keys = ("CycleCapacitySizePrecise", "CycleCapacitySize",
            "CapacitySizePrecise", "CapacitySize",
            "CycleCapacityRemainPrecise", "CycleCapacityRemain",
            "CapacityRemainPrecise", "CapacityRemain")
    for a in accounts:
        if not isinstance(a, dict):
            continue
        for k in keys:
            v = a.get(k)
            if v is None:
                continue
            try:
                float(str(v).strip())
                return True
            except (ValueError, TypeError):
                continue
    return False


class TokenRotator:
    """粘性优先账号池：锁定一个主账号持续使用，直到它额度耗尽/出错，
    再切到下一个。未被选中的账号保持干净（不轮询消耗）。

    账号状态：
      - quota     额度耗尽（14018/429），冷却 1h
      - auth      非法请求（403），冷却 10min
      - transient 临时错误（401/502/503/504/超时），冷却 1min
      - disabled/banned  由配额API判定持久化，本类不可覆盖
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._accounts: list[Account] = []
        self._index = 0
        self._current_id: Optional[str] = None  # 当前粘性锁定的账号
        self._pending_switch_from: Optional[str] = None  # 被标记不可用的旧号（写切号日志用）
        self._pending_switch_from_nick: str = ""
        self._pending_switch_reason: str = ""  # 暂存的切号原因（on_disable 等不走 mark_disabled 的路径）
        self._active_count: int = 0  # 并发请求计数
        # id -> {"reason": str, "until": float}
        self._disabled: dict[str, dict] = {}
        # id -> 估算剩余额度（从 quota_raw 初始化，每次请求扣减）
        self._estimated_remain: dict[str, float] = {}
        # 估算有效集合：仅当最近一次容量快照成功解析（quota_raw 可用）才纳入。
        # 估算无效/未知的账号，deduct_quota 不会用它触发阈值切号 —— 宁可多用一个号，
        # 也不要因为快照解析失败把「剩余额度=0」误判成耗尽而提前切号。
        # 真实额度彻底耗尽由上游 429/14008 → mark_disabled("quota") 兜底处理。
        self._estimate_valid: set[str] = set()
        self._threshold: float = 0.0
        self._enable_threshold: float = 0.0
        self._platform: str = "workbuddy"
        # id -> 连续 11140 次数（banned 渐进冷却累计，5 次落库封禁）
        self._banned_fail: dict[str, int] = {}
        self._threshold_switch: Optional[str] = None
        self._threshold_no_fallback: bool = False
        self._threshold_no_fallback_id: Optional[str] = None

    def reload(self, platform: str, calibrate: bool = False):
        with self._lock:
            self._platform = platform
            all_accs = store.list_accounts(platform)
            self._accounts = [a for a in all_accs if a.access_token]
            if self._index >= len(self._accounts):
                self._index = 0

            self._restore_cooldowns()
            self._restore_estimates()
            self._load_threshold()
            self._load_enable_threshold()
            self._refresh_estimates(calibrate)
            self._threshold_no_fallback = False

            # 校验当前锁定是否仍有效
            if self._current_id:
                cur = next((a for a in self._accounts if a.id == self._current_id), None)
                if not cur or not self._is_usable(cur):
                    self._current_id = None

            # 恢复持久化的优先账号
            if not self._current_id:
                saved = store.get_setting("priority_account", "")
                if saved:
                    acc = next((a for a in self._accounts if a.id == saved), None)
                    if acc and self._is_usable(acc):
                        self._current_id = saved

            # 都没有就自动选第一个可用账号
            if not self._current_id:
                for acc in self._accounts:
                    if self._is_usable(acc):
                        self._current_id = acc.id
                        break

    def _is_usable(self, acc: Account) -> bool:
        if not acc.access_token:
            return False
        if acc.status in ("disabled", "banned"):
            return False
        if acc.expires_at and acc.expires_at < int(time.time()) + 60:
            return False
        st = self._disabled.get(acc.id)
        if st:
            until = st.get("until")
            if until is None or until > time.time():
                return False
            # 冷却到期，清除
            self._disabled.pop(acc.id, None)
        return True

    def ensure_loaded(self, platform: str):
        """池为空时先加载。

        给调用方在 count_usable() 之前用 —— 否则「池未加载」和「全部不可用」
        都返回 0，重试次数会被算成 1，明明有 N 个号也只试一次。
        """
        with self._lock:
            if not self._accounts:
                self.reload(platform)

    def count(self) -> int:
        """池内账号总数（不判断可用性）。"""
        with self._lock:
            return len(self._accounts)

    def get_next(self, platform: str) -> Optional[Account]:
        """粘性优先：优先返回当前锁定账号；不可用时才找下一个可用账号。"""
        switch_row = None  # 锁外写 switch_log（store 是同步 sqlite，锁内写卡事件循环）
        with self._lock:
            if not self._accounts:
                self.reload(platform)
            if not self._accounts:
                return None

            # 1. 当前锁定的账号仍可用 → 继续用它
            if self._current_id:
                cur = next((a for a in self._accounts if a.id == self._current_id), None)
                if cur and self._is_usable(cur):
                    return cur

            # 2. 找下一个可用账号
            prev_id = self._current_id or self._pending_switch_from or ""
            prev_nick = ""
            if prev_id == self._pending_switch_from:
                prev_nick = self._pending_switch_from_nick
            elif prev_id:
                pa = next((a for a in self._accounts if a.id == prev_id), None)
                prev_nick = pa.nickname or "" if pa else ""
            n = len(self._accounts)
            result = None
            for _ in range(n):
                if self._index >= n:
                    self._index = 0
                acc = self._accounts[self._index]
                self._index = (self._index + 1) % n
                if self._is_usable(acc):
                    self._current_id = acc.id
                    if prev_id and prev_id != acc.id and platform == self._platform:
                        # 收集参数，锁外再写库。原因优先取暂存的（on_disable 等不走
                        # mark_disabled 的路径），否则从 _disabled 冷却记录推断
                        reason = self._pending_switch_reason or self._switch_reason(prev_id)
                        switch_row = (platform, prev_id, prev_nick,
                                      acc.id, acc.nickname or "", reason)
                    self._pending_switch_from = None
                    self._pending_switch_from_nick = ""
                    self._pending_switch_reason = ""
                    result = acc
                    break
            if result is None:
                # 所有可用账号耗尽：轮转探测 transient 限流账号。
                # until=None 无限期，上游解除与否未知 —— 用真实请求试：
                # 成功（代理层收到数据后调 clear_disabled）解除限流，
                # 失败（又超时/报错）mark_disabled 重新标记，继续限流。
                # 从当前轮转位置开始扫（不是固定第一个）—— 多个限流号时
                # 每次请求探测不同号，配合代理层 tried_ids 去重，单请求内
                # 所有号各试一次，不会重复轰炸同一个号。
                n2 = len(self._accounts)
                for i in range(n2):
                    acc = self._accounts[(self._index + i) % n2]
                    st = self._disabled.get(acc.id)
                    if st and st.get("reason") == "transient" and acc.status == "normal":
                        self._current_id = acc.id
                        self._index = (self._index + i + 1) % n2
                        result = acc
                        break
            if result is None:
                self._pending_switch_from = None
                self._pending_switch_from_nick = ""
                self._pending_switch_reason = ""
        if switch_row:
            try:
                store.add_switch_log(*switch_row)
            except Exception:
                pass
        return result

    def _switch_reason(self, account_id: str) -> str:
        """取切号原因：优先冷却记录中的 reason，否则默认。"""
        st = self._disabled.get(account_id)
        if st:
            r = st.get("reason")
            if r:
                return {"quota": "额度耗尽", "auth": "非法请求", "transient": "临时错误", "banned": "封号", "manual": "手动禁用"}.get(r, r)
        return ""

    def mark_disabled(self, account_id: str, reason: str):
        """标记账号不可用，自动持久化冷却记录到 settings 表。

        banned（11140 封号）：渐进冷却 30s→1m→2m→5m→10m，给临时风控恢复机会；
        连续第 5 次在 DB 标记 status='banned'（永久隔离）+ reload 立即生效。
        落库/reload 放到锁外 —— store 是同步 sqlite，锁内写库会卡住事件循环。
        """
        to_persist = None
        with self._lock:
            if reason == "banned":
                n = self._banned_fail.get(account_id, 0) + 1
                self._banned_fail[account_id] = n
                if n >= 5:
                    acc = next((a for a in self._accounts if a.id == account_id), None)
                    if acc:
                        acc.status = "banned"
                        to_persist = acc
                    self._disabled.pop(account_id, None)
                    self._banned_fail.pop(account_id, None)
                else:
                    cd = BANNED_COOLDOWNS[min(n - 1, len(BANNED_COOLDOWNS) - 1)]
                    self._disabled[account_id] = {"reason": "banned", "until": time.time() + cd}
            else:
                # transient（超时/DNS/临时错误）：上游何时解除未知，60s 只是猜测。
                # 改为无限期限流（until=None）—— 恢复的唯一途径是真实请求探测：
                # 所有可用账号耗尽时轮询限流账号发请求，成功（代理层调
                # clear_disabled）才解除，失败继续保持限流。宁可多等，不盲目信任。
                self._disabled[account_id] = {
                    "reason": reason,
                    "until": None,
                }
            if account_id == self._current_id:
                # 当前号被标记不可用：暂存原号与原因，供 get_next 换号时写切号日志
                self._pending_switch_from = account_id
                self._pending_switch_from_nick = ""
                pa = next((a for a in self._accounts if a.id == account_id), None)
                if pa:
                    self._pending_switch_from_nick = pa.nickname or ""
                self._current_id = None
        self._persist_cooldowns()
        if to_persist is not None:
            try:
                store.upsert_account(self._platform, to_persist)
            except Exception as e:
                logger.warning("[调度] banned 标记持久化失败: %r", e)
            try:
                self.reload(self._platform)
            except Exception:
                pass

    def set_active(self, active: bool):
        """标记网关是否正在处理请求（用于 UI 边框动画）。"""
        with self._lock:
            if active:
                self._active_count += 1
            else:
                self._active_count = max(0, self._active_count - 1)

    def set_priority(self, account_id: str):
        """手动设置优先调度账号，下次 get_next 优先使用它。"""
        with self._lock:
            self._current_id = account_id

    def clear_disabled(self, account_id: str):
        """清除指定账号的所有运行时冷却（手动启用/验活通过/探测成功时调用）。
        内存 + 库里该账号的记录一起删（_persist_cooldowns 是合并写，
        单靠它会把库里旧记录捞回来，必须显式从库里删这条）。"""
        with self._lock:
            self._disabled.pop(account_id, None)
            self._banned_fail.pop(account_id, None)
        try:
            raw = store.get_setting(_COOLDOWN_SETTING_KEY, "")
            if raw:
                saved = json.loads(raw)
                if account_id in saved:
                    del saved[account_id]
                    store.save_setting(_COOLDOWN_SETTING_KEY, json.dumps(saved, ensure_ascii=False))
        except Exception:
            pass

    def on_disable(self, account_id: str) -> bool:
        """手动禁用账号后，若它是当前号则切换到下一个可用号。
        若禁用后无可用账号则拒绝操作，返回 False。
        不直接选号 —— 清 current + 暂存旧号，下次 get_next 用轮转逻辑选号并写切号日志。"""
        with self._lock:
            usable_without = [a for a in self._accounts if a.id != account_id and self._is_usable(a)]
            if not usable_without:
                return False
            disabled_acc = None
            for a in self._accounts:
                if a.id == account_id:
                    a.status = "disabled"
                    disabled_acc = a
                    break
            if account_id == self._current_id:
                # 暂存旧号 + 原因，下次 get_next 换号时写日志。
                # 不往 _disabled 塞临时记录（会被 _is_usable 到期 pop 掉导致 reason 丢失），
                # 用独立的 _pending_switch_reason 存。
                self._pending_switch_from = account_id
                self._pending_switch_from_nick = disabled_acc.nickname or "" if disabled_acc else ""
                self._pending_switch_reason = "手动禁用"
                self._current_id = None
            return True

    def count_usable(self) -> int:
        with self._lock:
            return sum(1 for a in self._accounts if self._is_usable(a))

    def count_total(self) -> int:
        """池中账号总数（含限流/冷却中的）—— 代理层 failover 重试上限用：
        正常号 + 限流探测号都算，保证限流号也有机会被探测。"""
        with self._lock:
            return len(self._accounts)

    def has_usable_besides(self, account_ids) -> bool:
        """删除/禁用前置检查：除了给定账号外，池中是否还有可用账号。

        与 on_disable 共用同一套可用性判定（_is_usable），但只查不改 ——
        删除路径要先把 DB 删掉再 reload，这里不能提前改内存状态。
        返回 False 表示删完会没有任何可用账号，调用方应拒绝操作。
        """
        # ensure_loaded 让 count_usable / _is_usable 在池未加载时也能给出正确答案，
        # 否则「池空」和「未加载」无法区分，会把 N 个号算成 0。
        if not self._accounts:
            return False
        bad = set(account_ids) if not isinstance(account_ids, str) else {account_ids}
        with self._lock:
            return any(a.id not in bad and self._is_usable(a) for a in self._accounts)

    def deduct_quota(self, account_id: str, amount: float):
        """请求成功后扣减估算额度，低于阈值则自动禁用并切号。
        若池中无可换的号则不切号不禁用，只第一次弹出 warn/prompt。"""
        # 锁内只做内存状态判定，落库放到锁外 —— store.* 是同步 sqlite，
        # 默认 timeout=5s，GUI 线程并发写库时会连带把这把锁和事件循环一起卡住。
        to_persist = None    # 需要落库的 Account
        log_row = None       # (level, account_id, nickname, message)
        switch_row = None    # (from_id, from_nick, to_id, to_nick, reason) 锁外写

        with self._lock:
            # 缺失估算值时按 0 兜底而不是 return，否则这个账号永不扣减、永不触发阈值
            if account_id not in self._estimated_remain:
                self._estimated_remain[account_id] = 0.0
            self._estimated_remain[account_id] = max(0, self._estimated_remain[account_id] - amount)
            if self._threshold <= 0 or self._estimated_remain[account_id] >= self._threshold:
                return
            # 快照不可用的账号估算不可信：不触发阈值切号，
            # 宁可多用一个号，也不把「快照解析失败」误判成额度耗尽。
            # 真实耗尽由上游 429/14018 → mark_disabled("quota") 兜底处理。
            if account_id not in self._estimate_valid:
                return

            acc = next((a for a in self._accounts if a.id == account_id), None)
            if not acc or acc.status != "normal":
                return

            # 备选必须"可用且剩余额度>=阈值"，否则切过去又触发阈值→来回震荡
            good = [a for a in self._accounts
                    if a.id != account_id
                    and self._is_usable(a)
                    and self._estimated_remain.get(a.id, 0) >= self._threshold]
            if not good:
                if not self._threshold_no_fallback:
                    self._threshold_no_fallback = True
                    self._threshold_switch = "__nofallback__" + (acc.nickname or account_id)
                    log_row = ("warning", account_id, acc.nickname or "",
                               "额度低于阈值且无额度充足的备选账号，继续使用当前号")
            else:
                acc.status = "disabled"
                to_persist = acc
                self._current_id = good[0].id
                self._threshold_switch = acc.nickname or account_id
                # 成功换号说明又有可用备选了，复位标志，
                # 否则第一次 nofallback 之后所有后续提示都会被静默吃掉
                self._threshold_no_fallback = False
                reason = f"额度低于阈值({self._threshold})"
                log_row = ("warning", account_id, acc.nickname or "",
                           f"{reason}，已自动禁用并切换")
                # 切号日志参数收集到锁外再写（store.add_switch_log 是同步 sqlite，
                # 锁内写会卡住网关事件循环）
                switch_row = (account_id, acc.nickname or "",
                              good[0].id, good[0].nickname or "", reason)

        if to_persist is not None:
            try:
                store.upsert_account(self._platform, to_persist)
            except Exception as e:
                logger.warning("[调度] 阈值换号持久化失败: %r", e)
        if switch_row:
            try:
                store.add_switch_log(self._platform, *switch_row)
            except Exception:
                pass
        if log_row:
            level, aid, nick, msg = log_row
            try:
                store.add_log(level, self._platform, aid, nick, "", msg, "")
            except Exception:
                pass

    def set_threshold(self, value: float):
        with self._lock:
            self._threshold = value
            try:
                store.save_setting(_THRESHOLD_SETTING_KEY, str(value))
            except Exception:
                pass

    def get_threshold(self) -> float:
        with self._lock:
            if self._threshold == 0.0:
                self._load_threshold()
            return self._threshold

    def _load_threshold(self):
        try:
            self._threshold = float(store.get_setting(_THRESHOLD_SETTING_KEY, "0") or "0")
        except (ValueError, TypeError):
            self._threshold = 0.0

    def set_enable_threshold(self, value: float):
        with self._lock:
            self._enable_threshold = value
            try:
                store.save_setting(_ENABLE_THRESHOLD_SETTING_KEY, str(value))
            except Exception:
                pass

    def get_enable_threshold(self) -> float:
        with self._lock:
            if self._enable_threshold == 0.0:
                self._load_enable_threshold()
            return self._enable_threshold

    def _load_enable_threshold(self):
        try:
            self._enable_threshold = float(
                store.get_setting(_ENABLE_THRESHOLD_SETTING_KEY, "0") or "0")
        except (ValueError, TypeError):
            self._enable_threshold = 0.0

    def _refresh_estimates(self, calibrate: bool = False):
        """重算每个账号的估算剩余额度。

        每个账号单独 try —— calc_totals 会对 quota_raw 里 {"data": null} 这类
        形状抛 AttributeError（配额接口报错时完全可能）。原先整个循环裸奔，
        一个账号解析失败就会让它后面所有账号缺失估算值，而 deduct_quota 对
        缺失的 key 直接 return，那些账号从此永不扣减、永不触发阈值。

        统一策略（calibrate 仅作语义标记，防抬高逻辑一致）：
          - prev 缺失或为 0：信任 fresh（prev==0 可能是旧版 Status=3 误计压低的
            坏值，手动刷新就是要把它校准回真实值）。
          - prev>0：取 min(fresh, prev)。运行期内存值是已扣减的真实消耗，上游
            快照可能因结算延迟偏高 —— 取小防估算被抬高导致超用。
            这点对 calibrate=True（手动刷新）同样成立：用户点刷新时网关可能在
            跑，刚扣的几次请求上游未必已结算，直接覆盖会把估算抬回去。
          - 快照不可用（_snapshot_usable=False）：不覆盖内存估算（保留运行期
            usage 扣减出的准确值），并移出 _estimate_valid —— 这类账号不触发
            阈值切号，宁可多用一个号，也不把「快照解析失败」误判成额度耗尽。
            真实耗尽由上游 429/14018 → mark_disabled("quota") 兜底。
        """
        self._estimate_valid.clear()
        for acc in self._accounts:
            try:
                total, used = calc_totals(acc.quota_raw, acc.usage_raw)
                fresh = max(0, total - used)
                if not _snapshot_usable(acc.quota_raw, acc.usage_raw):
                    self._estimate_valid.discard(acc.id)
                    continue
                self._estimate_valid.add(acc.id)
                prev = self._estimated_remain.get(acc.id)
                if prev is None or prev == 0:
                    self._estimated_remain[acc.id] = fresh
                else:
                    self._estimated_remain[acc.id] = min(fresh, prev)
            except Exception as e:
                logger.warning("[调度] 账号=%s 额度估算失败: %r", acc.nickname or acc.id, e)
                self._estimate_valid.discard(acc.id)

    def _persist_cooldowns(self):
        """合并写库：内存记录与 settings 表已有记录合并后写入，不整表覆盖。
        多进程共用一个库（GUI + 外部脚本），各自内存可能只有部分记录，
        整表覆盖会互相清掉对方写的记录。"""
        with self._lock:
            now = time.time()
            # until=None 是无限期限流（transient 探测制），也要持久化；过期的丢弃
            mine = {aid: s for aid, s in self._disabled.items()
                    if s.get("until") is None or s.get("until", 0) > now}
        try:
            # 锁外合并：库里的记录（可能是别的进程/外部写入的）不被本进程内存覆盖
            raw = store.get_setting(_COOLDOWN_SETTING_KEY, "")
            merged = {}
            if raw:
                try:
                    for aid, s in json.loads(raw).items():
                        if s.get("until") is None or s.get("until", 0) > now:
                            merged[aid] = s
                except Exception:
                    pass
            # 本进程刚 clear_disabled 的账号要真正从库里删掉：内存没有 = 不在 merged，
            # 但库里可能有旧记录 → 用「内存明确清除过的」做差集。简单起见：
            # 本进程知道的全量账号 ID 里，内存已无记录且库里有 → 删。
            # （clear_disabled 已单独处理落库，这里只负责不覆盖别人的记录）
            merged.update(mine)
            store.save_setting(_COOLDOWN_SETTING_KEY, json.dumps(merged, ensure_ascii=False))
        except Exception:
            pass

    def _restore_cooldowns(self):
        try:
            raw = store.get_setting(_COOLDOWN_SETTING_KEY, "")
            if not raw:
                return
            saved = json.loads(raw)
            now = time.time()
            for aid, s in saved.items():
                until = s.get("until")
                # until=None 是无限期限流（transient 探测制），照常恢复
                if until is None or until > now:
                    self._disabled[aid] = s
        except Exception:
            pass

    def persist_estimates(self):
        """关闭网关时把内存估算值一次性落库，重启后恢复，防止丢扣减记录。

        运行期间纯内存扣减（零 IO），只在停机/退出时写一次 settings 表。
        重启后 reload → _restore_estimates 恢复 → _refresh_estimates 取
        min(恢复值, DB快照)，保证估算不被陈旧 DB 快照抬高。
        """
        with self._lock:
            if not self._estimated_remain:
                return
            payload = json.dumps(self._estimated_remain, ensure_ascii=False)
        try:
            store.save_setting(_ESTIMATE_SETTING_KEY, payload)
        except Exception:
            pass

    def _restore_estimates(self):
        """从 settings 恢复上次关闭时持久化的估算值（在 _refresh_estimates 之前调用）。

        仅在刚刚启动（内存估算为空）时恢复 —— 运行期 reload 时内存里已有
        运行期扣减过的真实值，若再用持久化快照覆盖，会把运行期的扣减吞掉
        （表现为 reload 后估算被抬高、提前切号）。"""
        if self._estimated_remain:
            return
        try:
            raw = store.get_setting(_ESTIMATE_SETTING_KEY, "")
            if not raw:
                return
            saved = json.loads(raw)
            if isinstance(saved, dict):
                for aid, val in saved.items():
                    try:
                        self._estimated_remain[aid] = float(val)
                    except (ValueError, TypeError):
                        pass
        except Exception:
            pass

    def status(self) -> dict:
        with self._lock:
            now = time.time()
            sw = self._threshold_switch
            self._threshold_switch = None
            return {
                "total": len(self._accounts),
                "usable": sum(1 for a in self._accounts if self._is_usable(a)),
                "current": self._current_id,
                "active": self._active_count > 0,
                "threshold_switch": sw,
                "disabled": [
                    {"id": aid, "reason": s.get("reason"), "until": s.get("until")}
                    for aid, s in self._disabled.items()
                    if s.get("until") is None or s.get("until") > now
                ],
            }


token_rotator = TokenRotator()
