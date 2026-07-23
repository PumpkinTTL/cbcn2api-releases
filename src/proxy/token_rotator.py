import time
import threading
from typing import Optional

from src.storage import store
from src.models.account import Account


class TokenRotator:
    def __init__(self):
        self._lock = threading.RLock()
        self._accounts: list[Account] = []
        self._index = 0

    def reload(self, platform: str):
        with self._lock:
            all_accs = store.list_accounts(platform)
            valid = [a for a in all_accs if self._is_token_valid(a)]
            self._accounts = valid
            if self._index >= len(self._accounts):
                self._index = 0

    def get_next(self, platform: str) -> Optional[Account]:
        with self._lock:
            if not self._accounts:
                self.reload(platform)
            if not self._accounts:
                return None
            if self._index >= len(self._accounts):
                self._index = 0
            acc = self._accounts[self._index]
            self._index = (self._index + 1) % len(self._accounts)
            if not self._is_token_valid(acc):
                self.reload(platform)
                if not self._accounts:
                    return None
                self._index = self._index % len(self._accounts)
                return self._accounts[self._index]
            return acc

    def count(self) -> int:
        with self._lock:
            return len(self._accounts)

    def _is_token_valid(self, acc: Account) -> bool:
        if not acc.access_token:
            return False
        if acc.expires_at and acc.expires_at < int(time.time()) + 60:
            return False
        return True


token_rotator = TokenRotator()
