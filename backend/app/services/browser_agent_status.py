"""服务器端归一化浏览器 Agent 状态，短缓存且不透传远端细节。"""

from collections.abc import Callable
from datetime import UTC, datetime
from time import monotonic
from typing import Literal, cast

from pydantic import Field

from app.adapters.remote_browser import AgentClient, current_browser_device_id
from app.domain.errors import DomainError
from app.domain.schemas import Contract


class BrowserAgentStatusView(Contract):
    state: Literal["offline", "online", "busy", "attention_required"]
    operation: str = ""
    operation_id: str = ""
    started_at: datetime | None = None
    last_error_code: str = ""
    version: str = ""
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class BrowserAgentStatusService:
    def __init__(
        self,
        client: AgentClient | None,
        *,
        cache_seconds: float = 5.0,
        clock: Callable[[], float] = monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.client = client
        self.cache_seconds = cache_seconds
        self.clock = clock
        self.now = now
        self._cache: dict[str, tuple[float, BrowserAgentStatusView]] = {}

    def get(self, *, refresh: bool = False) -> BrowserAgentStatusView:
        stamp = self.clock()
        device_id = current_browser_device_id()
        cached = self._cache.get(device_id)
        if (
            not refresh
            and cached is not None
            and stamp - cached[0] < self.cache_seconds
        ):
            return cached[1]
        checked_at = self.now()
        if self.client is None:
            status = BrowserAgentStatusView(
                state="offline",
                last_error_code="AGENT_NOT_CONFIGURED",
                checked_at=checked_at,
            )
        else:
            try:
                raw = self.client.call("GET", "/v1/status")
                safe = cast(dict[str, object], raw)
                status = BrowserAgentStatusView.model_validate(
                    {
                        "state": safe.get("state", "online"),
                        "operation": str(safe.get("operation", "")),
                        "operation_id": str(safe.get("operation_id", "")),
                        "started_at": safe.get("started_at"),
                        "last_error_code": str(safe.get("last_error_code", "")),
                        "version": str(safe.get("version", "")),
                        "checked_at": checked_at,
                    }
                )
            except (DomainError, ValueError) as error:
                status = BrowserAgentStatusView(
                    state="offline",
                    last_error_code=(
                        error.code if isinstance(error, DomainError) else "AGENT_INVALID_RESPONSE"
                    ),
                    checked_at=checked_at,
                )
        self._cache[device_id] = (stamp, status)
        return status
