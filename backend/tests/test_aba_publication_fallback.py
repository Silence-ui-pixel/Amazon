"""ABA 最新周尚未发布时，自动回退到上一份可用周报。"""

from collections.abc import Callable
from datetime import date

import pytest

from app.domain.aba import AbaConnectionStatus, AbaQueryReport
from app.domain.errors import DomainError
from app.services.aba import fetch_product_term_reports


class Provider:
    def __init__(self, behavior: Callable[[str, str], AbaQueryReport]) -> None:
        self.behavior = behavior
        self.calls: list[tuple[str, str]] = []

    def fetch(
        self,
        query: str,
        week_end: str,
        *,
        country: str = "us",
        max_rows: int = 1000,
        query_mode: str = "root",
        on_progress: object = None,
    ) -> AbaQueryReport:
        del country, max_rows, query_mode, on_progress
        self.calls.append((query, week_end))
        return self.behavior(query, week_end)

    def status(self) -> AbaConnectionStatus:
        return AbaConnectionStatus(credentials_present=True, cdp_configured=True)


def report(query: str) -> AbaQueryReport:
    return AbaQueryReport(
        query=query,
        query_mode="product_term",
        total=0,
        fetched=0,
        pages=1,
        truncated=False,
        stop_reason="complete",
        rows=[],
    )


def test_unpublished_latest_week_falls_back_one_week() -> None:
    def behavior(query: str, week: str) -> AbaQueryReport:
        if week == "2026-10-31":
            raise DomainError(502, "ABA_UPSTREAM_ERROR", "ABA 服务暂时不可用")
        return report(query)

    provider = Provider(behavior)

    week, reports, warning = fetch_product_term_reports(
        provider, ["shoe rack", "shoe organizer"], date(2026, 10, 31), 1000
    )

    assert week == date(2026, 10, 24)
    assert [item.query for item in reports] == ["shoe rack", "shoe organizer"]
    assert provider.calls == [
        ("shoe rack", "2026-10-31"),
        ("shoe rack", "2026-10-24"),
        ("shoe organizer", "2026-10-24"),
    ]
    assert "2026-10-31" in warning and "2026-10-24" in warning


def test_permission_error_never_falls_back_to_another_week() -> None:
    provider = Provider(
        lambda _query, _week: (_ for _ in ()).throw(
            DomainError(503, "ABA_ACCESS_DENIED", "ABA 拒绝访问")
        )
    )

    with pytest.raises(DomainError) as error:
        fetch_product_term_reports(provider, ["shoe rack"], date(2026, 10, 31), 1000)

    assert error.value.code == "ABA_ACCESS_DENIED"
    assert provider.calls == [("shoe rack", "2026-10-31")]
