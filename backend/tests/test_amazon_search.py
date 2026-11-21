"""搜索卡片广告过滤、分页和异常页必须可区分。"""

import pytest

from app.domain.errors import DomainError
from app.services.amazon_search import parse_snapshot


def test_sponsored_excluded_and_positions_count_only_organic() -> None:
    page = parse_snapshot(
        {
            "region": "Deliver to United States",
            "ready": True,
            "next_page": False,
            "pagination_known": True,
            "cards": [
                {
                    "asin": "B000000001",
                    "image_url": "https://m.media-amazon.com/images/a.jpg",
                    "sponsored": True,
                },
                {
                    "asin": "B000000002",
                    "image_url": "https://m.media-amazon.com/images/b.jpg",
                    "sponsored": False,
                },
                {"asin": "B000000003", "image_url": "", "sponsored": False},
            ],
        }
    )
    assert [(c.asin, c.position) for c in page.cards] == [("B000000002", 1), ("B000000003", 2)]


def test_unparsed_or_region_missing_never_means_not_found() -> None:
    examples: list[object] = [{}, {"ready": True, "region": "", "cards": []}]
    for raw in examples:
        with pytest.raises(DomainError):
            parse_snapshot(raw)


def test_wrong_or_unconfirmed_page_number_is_rejected() -> None:
    raw = {
        "region": "United States",
        "ready": True,
        "pagination_known": True,
        "next_page": False,
        "page_number": 1,
        "cards": [{"asin": "B000000002", "sponsored": False}],
    }
    with pytest.raises(DomainError, match="页码"):
        parse_snapshot(raw, expected_page=2)


@pytest.mark.parametrize("region", ["New York 10001", "New York, NY 10001", "10001"])
def test_us_delivery_accepts_confirmed_zip(region: str) -> None:
    from app.services.amazon_search import require_us_delivery

    require_us_delivery(region, "10001")


@pytest.mark.parametrize(
    "region", ["China", "United States", "New York 10002", "China 10001", "110001"]
)
def test_us_delivery_rejects_unconfirmed_or_changed_destination(region: str) -> None:
    from app.services.amazon_search import require_us_delivery

    with pytest.raises(DomainError) as error:
        require_us_delivery(region, "10001")
    assert error.value.code == "SEARCH_DELIVERY_UNCONFIRMED"
