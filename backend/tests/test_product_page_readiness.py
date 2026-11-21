"""Regression for a streamed product title arriving before the ASIN and details."""

import unittest
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from playwright.sync_api import TimeoutError as BrowserTimeout

from app.domain.errors import DomainError
from app.services import asin_product as module


class StreamingProductPage:
    def __init__(self, *, found: str = "B0F94KSVBJ", blocked: bool = False) -> None:
        self.url = "https://www.amazon.co.uk/dp/B0F94KSVBJ"
        self.document_loaded = False
        self.found = found
        self.blocked = blocked
        self.context = SimpleNamespace(cookies=lambda urls: [])

    def wait_for_load_state(self, state: str, **kwargs: object) -> None:
        if state != "domcontentloaded":
            raise AssertionError("Details must wait for the document body")
        self.document_loaded = True

    def wait_for_function(self, expression: str, **kwargs: object) -> None:
        # A title is already available in the initial streamed response.
        pass

    def evaluate(self, expression: str) -> object:
        if expression.startswith("!!document.querySelector('#captchacharacters"):
            return self.blocked
        if expression != module.EXTRACT_PRODUCT:
            raise AssertionError("Unexpected extraction")
        return {
            "asin": self.found if self.document_loaded else "",
            "title": "Plate storage rack",
            "features": ["Heavy duty steel"] if self.document_loaded else [],
            "price_text": "GBP 39.99" if self.document_loaded else "",
            "currency": "GBP",
            "blocked": self.blocked,
        }


class ProductPageReadinessTests(unittest.TestCase):
    def test_batch_reader_waits_for_body_before_extracting(self) -> None:
        product = module.read_product_page(StreamingProductPage(), "UK", "B0F94KSVBJ")
        self.assertEqual(product.asin, "B0F94KSVBJ")
        self.assertEqual(product.features, ["Heavy duty steel"])
        self.assertEqual(product.price, Decimal("39.99"))

    def test_one_shot_reader_uses_the_same_readiness_gate(self) -> None:
        @contextmanager
        def opened(*args: object):
            yield StreamingProductPage()

        with patch.object(module, "amazon_product_page", opened):
            product = module.AmazonProductProvider(Path(".")).fetch("UK", "B0F94KSVBJ")
        self.assertEqual(product.features, ["Heavy duty steel"])
        self.assertEqual(product.price, Decimal("39.99"))

    def test_loaded_different_asin_is_still_rejected(self) -> None:
        with self.assertRaises(DomainError) as caught:
            module.read_product_page(
                StreamingProductPage(found="B000000001"), "UK", "B0F94KSVBJ"
            )
        self.assertEqual(caught.exception.code, "ASIN_IDENTITY_UNCONFIRMED")
        self.assertIn("B000000001", caught.exception.message)

    def test_missing_identity_is_not_reported_as_a_different_product(self) -> None:
        with self.assertRaises(DomainError) as caught:
            module.parse_product_snapshot({"asin": "", "title": "Rack"}, "B0F94KSVBJ")
        self.assertEqual(caught.exception.code, "ASIN_DETAILS_UNAVAILABLE")

    def test_document_timeout_is_retryable(self) -> None:
        page = StreamingProductPage()
        with (
            patch.object(page, "wait_for_load_state", side_effect=BrowserTimeout("loading")),
            self.assertLogs(module.logger, level="ERROR"),
            self.assertRaises(DomainError) as caught,
        ):
            module.read_product_page(page, "UK", "B0F94KSVBJ")
        self.assertEqual(caught.exception.code, "ASIN_PAGE_TIMEOUT")

    def test_captcha_remains_an_access_check(self) -> None:
        with self.assertRaises(DomainError) as caught:
            module.read_product_page(
                StreamingProductPage(blocked=True), "UK", "B0F94KSVBJ"
            )
        self.assertEqual(caught.exception.code, "SEARCH_ACCESS_CHECK")


if __name__ == "__main__":
    unittest.main()
