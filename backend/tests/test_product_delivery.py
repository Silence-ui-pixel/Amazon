"""商品详情应确认美国配送并保留可靠的价格币种。"""

from collections.abc import Generator
from contextlib import contextmanager
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.domain.errors import DomainError
from app.services import asin_product as module


@pytest.mark.parametrize("region,should_pass", [("New York 10001", True), ("China", False)])
@pytest.mark.parametrize("ziniao", [False, True])
def test_us_product_requires_verified_delivery(
    monkeypatch: pytest.MonkeyPatch, region: str, should_pass: bool, ziniao: bool
) -> None:
    class Page:
        url = "https://www.amazon.com/dp/B0BY7WJK92"
        delivered = False
        context: SimpleNamespace

        def goto(self, *args: object, **kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(status=200)

        def reload(self, *args: object, **kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(status=200)

        def wait_for_function(self, *args: object, **kwargs: object) -> None:
            pass

        def evaluate(self, expression: str) -> object:
            if expression == module.EXTRACT_PRODUCT:
                return {
                    "asin": "B0BY7WJK92",
                    "title": "Gate caster",
                    "price_text": "$18.99" if self.delivered else "CNY 126.66",
                    "currency": "",
                    "features": [],
                }
            if "icp-touch-link-cop" in expression:
                return "$ USD - U.S. Dollar"
            return region if self.delivered else "China"

    page = Page()
    added: list[object] = []

    def add_cookies(values: object) -> None:
        added.append(values)

    def cookies(urls: list[str]) -> list[dict[str, str]]:
        return [{"name": "i18n-prefs", "value": "USD"}]

    page.context = SimpleNamespace(add_cookies=add_cookies, cookies=cookies)

    @contextmanager
    def fake_page(*args: object, **kwargs: object) -> Generator[tuple[Page, bool]]:
        yield page, ziniao

    monkeypatch.setattr(module, "amazon_page", fake_page)

    def configure(page: Page, zipcode: str) -> None:
        page.delivered = True

    monkeypatch.setattr(module, "configure_us_delivery", configure, raising=False)
    monkeypatch.setenv("KEYWORD_US_DELIVERY_ZIP", "10001")
    if should_pass:
        result = module.AmazonProductProvider().fetch("US", "B0BY7WJK92")
        assert result.price == Decimal("18.99")
        assert result.currency == "USD"
    else:
        with pytest.raises(DomainError) as exc:
            module.AmazonProductProvider().fetch("US", "B0BY7WJK92")
        assert exc.value.code == "SEARCH_DELIVERY_UNCONFIRMED"
    # 紫鸟店铺有自己的币种设置，只对独立浏览器写入 USD 偏好。
    assert bool(added) is not ziniao
