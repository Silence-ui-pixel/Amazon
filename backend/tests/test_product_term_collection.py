"""商品词自然商品采集：顺序、去重、上限与详情失败证据。"""

from decimal import Decimal

import pytest

from app.domain.asin_product import ProductSnapshot
from app.domain.errors import DomainError
from app.domain.verification import SearchCard, SearchPage
from app.services.product_term_collection import collect_natural_products


class FakeSession:
    def __init__(self) -> None:
        self.searches: list[int] = []
        self.products: list[str] = []

    def search(self, keyword: str, page_number: int, evidence_name: str) -> SearchPage:
        self.searches.append(page_number)
        start = 1 if page_number == 1 else 16
        asins = [f"B{index:09d}" for index in range(start, start + 20)]
        if page_number == 2:
            asins[0] = "B000000010"  # 跨页重复，不能重复计入。
        return SearchPage(
            region="New York 10001",
            cards=[SearchCard(asin=asin, position=i + 1) for i, asin in enumerate(asins)],
            next_page=page_number == 1,
        )

    def product(self, asin: str) -> ProductSnapshot:
        self.products.append(asin)
        if asin == "B000000005":
            raise RuntimeError("detail unavailable")
        return ProductSnapshot(asin=asin, title=asin, price=Decimal("20"), currency="USD")


def test_collects_unique_natural_products_across_pages_and_stops_at_30() -> None:
    session = FakeSession()
    result = collect_natural_products(session, "gate wheel", limit=30)
    assert result.unique_asins == [
        *(f"B{index:09d}" for index in range(1, 21)),
        *(f"B{index:09d}" for index in range(21, 31)),
    ]
    assert result.raw_natural_count == 40
    assert len(result.products) == 29
    assert result.failures[0].asin == "B000000005"
    assert session.searches == [1, 2]
    assert len(session.products) == 30


def test_uses_actual_count_when_search_has_no_next_page() -> None:
    class Short(FakeSession):
        def search(self, keyword: str, page_number: int, evidence_name: str) -> SearchPage:
            return SearchPage(
                region="New York 10001",
                cards=[SearchCard(asin="B000000001", position=1)],
                next_page=False,
            )

    result = collect_natural_products(Short(), "gate wheel")
    assert result.unique_asins == ["B000000001"]
    assert len(result.products) == 1


def test_agent_infrastructure_failure_is_not_recorded_as_missing_product() -> None:
    class Offline(FakeSession):
        def product(self, asin: str) -> ProductSnapshot:
            raise DomainError(503, "AGENT_UNAVAILABLE", "agent offline")

    with pytest.raises(DomainError, match="agent offline"):
        collect_natural_products(Offline(), "gate wheel", limit=1)
