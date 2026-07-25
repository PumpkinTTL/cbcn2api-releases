import time
import threading
from typing import Optional

from src.storage import store
from src.models.account import Account

# 冷却时长（秒）；None = 永久（直到进程重启）
QUOTA_COOLDOWN = 3600   # 额度耗尽：1 小时后重试
AUTH_COOLDOWN = 600     # 鉴权失败（403）：10 分钟后重试
TRANSIENT_COOLDOWN = 60  # 临时错误：1 分钟后重试

_COOLDOWNS = {
    "quota": QUOTA_COOLDOWN,
    "auth": AUTH_COOLDOWN,
    "transient": TRANSIENT_COOLDOWN,
    "banned": None,  # 401 封禁：本进程内永久不可用
}


class TokenRotator:
    """粘性优先账号池：锁定一个主账号持续使用，直到它额度耗尽/被封，
    再切到下一个。未被选中的账号保持干净（不轮询消耗）。

    账号状态：
      - quota     额度耗尽（14018），冷却 1h
      - auth      鉴权失败（403），冷却 10min
      - banned    封禁（401），本进程永久不可用
      - transient 临时错误（429/超时），冷却 1min
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._accounts: list[Account] = []
        self._index = 0
        self._current_id: Optional[str] = None  # 当前粘性锁定的账号
        self._active: bool = False  # 是否正在处理请求
        # id -> {"reason": str, "until": float|None}
        self._disabled: dict[str, dict] = {}

    def reload(self, platform: str):
        with self._lock:
            all_accs = store.list_accounts(platform)
            self._accounts = [a for a in all_accs if a.access_token]
            if self._index >= len(self._accounts):
                self._index = 0

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
        """标记账号不可用。banned/None 冷却 = 本进程永久。"""
        with self._lock:
            cd = _COOLDOWNS.get(reason, TRANSIENT_COOLDOWN)
            self._disabled[account_id] = {
                "reason": reason,
                "until": (time.time() + cd) if cd is not None else None,
            }
            # 当前主账号失效 → 清除锁定，下次 get_next 自动切下一个
            if account_id == self._current_id:
                self._current_id = None

    def set_active(self, active: bool):
        """标记网关是否正在处理请求（用于 UI 边框动画）。"""
        with self._lock:
            self._active = active

    def set_priority(self, account_id: str):
        """手动设置优先调度账号，下次 get_next 优先使用它。"""
        with self._lock:
            self._current_id = account_id

    def count(self) -> int:
        with self._lock:
            return len(self._accounts)

    def count_usable(self) -> int:
        with self._lock:
            return sum(1 for a in self._accounts if self._is_usable(a))

    def status(self) -> dict:
        with self._lock:
            now = time.time()
            return {
                "total": len(self._accounts),
                "usable": sum(1 for a in self._accounts if self._is_usable(a)),
                "current": self._current_id,
                "active": self._active,
                "disabled": [
                    {"id": aid, "reason": s.get("reason"), "until": s.get("until")}
                    for aid, s in self._disabled.items()
                    if s.get("until") is None or s.get("until") > now
                ],
            }


token_rotator = TokenRotator()
