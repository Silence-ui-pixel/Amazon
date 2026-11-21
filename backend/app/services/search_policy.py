"""搜索请求的有限退避；付费模型调用不进入此策略。"""

import time
from collections.abc import Callable

from app.domain.errors import DomainError
from app.domain.verification import SearchPage

PAUSE_CODES = {
    "SEARCH_ACCESS_CHECK",
    "SEARCH_RATE_LIMITED",
    "SEARCH_SERVICE_UNAVAILABLE",
    "AGENT_UNAVAILABLE",
    "AGENT_INVALID_RESPONSE",
    "AGENT_HTTP_ERROR",
    "AGENT_BROWSER_ERROR",
    "AGENT_FILE_MISSING",
    "AGENT_SESSION_NOT_FOUND",
}
TRANSIENT_CODES = {
    "SEARCH_NETWORK_TIMEOUT",
    "SEARCH_NETWORK_ERROR",
    "SEARCH_RATE_LIMITED",
    "SEARCH_SERVICE_UNAVAILABLE",
}


class SearchFailure(DomainError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        stage: str = "",
        screenshot: str = "",
        diagnostic: str = "",
        retry_after: float = 0,
    ) -> None:
        super().__init__(502, code, message)
        self.stage = stage
        self.screenshot = screenshot
        self.diagnostic = diagnostic
        self.retry_after = retry_after


def retry_search(
    operation: Callable[[int], SearchPage], *, sleep: Callable[[float], None] = time.sleep
) -> SearchPage:
    """初次请求加两次重试；超过五分钟的服务端等待交由暂停处理。"""
    for attempt in range(1, 4):
        try:
            return operation(attempt)
        except DomainError as error:
            if error.code not in TRANSIENT_CODES or attempt == 3:
                raise
            delay = max(
                (10, 30)[attempt - 1], error.retry_after if isinstance(error, SearchFailure) else 0
            )
            if delay > 300:
                raise
            sleep(delay)
    raise AssertionError("unreachable")
