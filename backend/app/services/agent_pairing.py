"""一次性配对码：页面生成、客户端消费，用来把某台电脑登记成采集设备。

为什么需要它：客户端首次运行时要拿到自己的 device_id 和 token，但此时它还没有任何凭据。
用页面签发的一次性短码换凭据，既不用在安装包里内置密钥，也避免谁都能往工作区塞设备。
"""

import secrets
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.domain.errors import DomainError

# 去掉容易看错的 0/O/1/I。
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8
DEFAULT_TTL = timedelta(minutes=10)


@dataclass
class _Entry:
    code: str
    expires_at: datetime
    used: bool = False
    device_id: str = ""


class PairingCodeStore:
    def __init__(self, ttl: timedelta = DEFAULT_TTL) -> None:
        self.ttl = ttl
        self.lock = threading.Lock()
        self.entries: dict[str, _Entry] = {}

    @staticmethod
    def _normalize(code: str) -> str:
        return "".join(ch for ch in code.upper() if ch.isalnum())

    def issue(self) -> tuple[str, datetime]:
        raw = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))
        code = f"{raw[:4]}-{raw[4:]}"
        expires_at = datetime.now(UTC) + self.ttl
        with self.lock:
            self._purge_locked()
            self.entries[self._normalize(code)] = _Entry(code=code, expires_at=expires_at)
        return code, expires_at

    def bind(self, code: str, device_id: str) -> None:
        """配对成功后把码和设备关联，页面凭码即可查询连接状态。"""
        with self.lock:
            entry = self.entries.get(self._normalize(code))
            if entry is not None:
                entry.device_id = device_id

    def lookup(self, code: str) -> str:
        with self.lock:
            entry = self.entries.get(self._normalize(code))
            return entry.device_id if entry is not None else ""

    def consume(self, code: str) -> None:
        key = self._normalize(code)
        with self.lock:
            self._purge_locked()
            entry = self.entries.get(key)
            if entry is None or entry.used:
                raise DomainError(
                    422, "PAIRING_CODE_INVALID", "配对码无效或已使用，请在页面上重新获取"
                )
            entry.used = True

    def _purge_locked(self) -> None:
        now = datetime.now(UTC)
        expired = [key for key, value in self.entries.items() if value.expires_at <= now]
        for key in expired:
            self.entries.pop(key, None)
