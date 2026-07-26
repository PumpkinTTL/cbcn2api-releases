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

_COOLDOWN_SETTING_KEY = "cooldowns"
_THRESHOLD_SETTING_KEY = "quota_threshold"

_COOLDOWNS = {
    "quota": QUOTA_COOLDOWN,
    "auth": AUTH_COOLDOWN,
    "transient": TRANSIENT_COOLDOWN,
}


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
        self._active_count: int = 0  # 并发请求计数
        # id -> {"reason": str, "until": float}
        self._disabled: dict[str, dict] = {}
        # id -> 估算剩余额度（从 quota_raw 初始化，每次请求扣减）
        self._estimated_remain: dict[str, float] = {}
        self._threshold: float = 0.0
        self._platform: str = "workbuddy"
        self._threshold_switch: Optional[str] = None
        self._threshold_no_fallback: bool = False
        self._threshold_no_fallback_id: Optional[str] = None

    def reload(self, platform: str):
        with self._lock:
            self._platform = platform
            all_accs = store.list_accounts(platform)
            self._accounts = [a for a in all_accs if a.access_token]
            if self._index >= len(self._accounts):
                self._index = 0

            self._restore_cooldowns()
            self._load_threshold()
            self._refresh_estimates()
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
            n = len(self._accounts)
            for _ in range(n):
                if self._index >= n:
                    self._index = 0
                acc = self._accounts[self._index]
                self._index = (self._index + 1) % n
                if self._is_usable(acc):
                    self._current_id = acc.id
                    return acc
            return None

    def mark_disabled(self, account_id: str, reason: str):
        """标记账号不可用，自动持久化冷却记录到 settings 表。"""
        with self._lock:
            cd = _COOLDOWNS.get(reason, TRANSIENT_COOLDOWN)
            self._disabled[account_id] = {
                "reason": reason,
                "until": time.time() + cd,
            }
            if account_id == self._current_id:
                self._current_id = None
        self._persist_cooldowns()

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
        """清除指定账号的所有运行时冷却（手动启用时调用）。"""
        with self._lock:
            self._disabled.pop(account_id, None)
        self._persist_cooldowns()

    def on_disable(self, account_id: str) -> bool:
        """手动禁用账号后，若它是当前号则自动切换到下一个可用号。
        若禁用后无可用账号则拒绝操作，返回 False。"""
        with self._lock:
            usable_without = [a for a in self._accounts if a.id != account_id and self._is_usable(a)]
            if not usable_without:
                return False
            for a in self._accounts:
                if a.id == account_id:
                    a.status = "disabled"
                    break
            if account_id == self._current_id:
                self._current_id = usable_without[0].id
            return True

    def count_usable(self) -> int:
        with self._lock:
            return sum(1 for a in self._accounts if self._is_usable(a))

    def deduct_quota(self, account_id: str, amount: float):
        """请求成功后扣减估算额度，低于阈值则自动禁用并切号。
        若池中无可换的号则不切号不禁用，只第一次弹出 warn/prompt。"""
        # 锁内只做内存状态判定，落库放到锁外 —— store.* 是同步 sqlite，
        # 默认 timeout=5s，GUI 线程并发写库时会连带把这把锁和事件循环一起卡住。
        to_persist = None    # 需要落库的 Account
        log_row = None       # (level, account_id, nickname, message)

        with self._lock:
            # 缺失估算值时按 0 兜底而不是 return，否则这个账号永不扣减、永不触发阈值
            if account_id not in self._estimated_remain:
                self._estimated_remain[account_id] = 0.0
            self._estimated_remain[account_id] = max(0, self._estimated_remain[account_id] - amount)
            if self._threshold <= 0 or self._estimated_remain[account_id] >= self._threshold:
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
                log_row = ("warning", account_id, acc.nickname or "",
                           f"额度低于阈值({self._threshold})，已自动禁用并切换")

        if to_persist is not None:
            try:
                store.upsert_account(self._platform, to_persist)
            except Exception as e:
                logger.warning("[调度] 阈值换号持久化失败: %r", e)
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

    def _refresh_estimates(self):
        """重算每个账号的估算剩余额度。

        每个账号单独 try —— calc_totals 会对 quota_raw 里 {"data": null} 这类
        形状抛 AttributeError（配额接口报错时完全可能）。原先整个循环裸奔，
        一个账号解析失败就会让它后面所有账号缺失估算值，而 deduct_quota 对
        缺失的 key 直接 return，那些账号从此永不扣减、永不触发阈值。
        """
        for acc in self._accounts:
            try:
                total, used = calc_totals(acc.quota_raw, acc.usage_raw)
                self._estimated_remain[acc.id] = max(0, total - used)
            except Exception as e:
                logger.warning("[调度] 账号=%s 额度估算失败: %r", acc.nickname or acc.id, e)
                self._estimated_remain.setdefault(acc.id, 0.0)

    def _persist_cooldowns(self):
        with self._lock:
            now = time.time()
            active = {aid: s for aid, s in self._disabled.items() if s.get("until", 0) > now}
            payload = json.dumps(active, ensure_ascii=False)
        try:
            store.save_setting(_COOLDOWN_SETTING_KEY, payload)
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
                until = s.get("until", 0)
                if until > now:
                    self._disabled[aid] = s
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
