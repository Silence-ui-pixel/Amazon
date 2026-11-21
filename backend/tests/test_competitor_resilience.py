"""Competitor filtering exposes per-ASIN outcomes and refreshes title-only cache."""
import unittest
from decimal import Decimal

from app.domain.asin_product import ProductSnapshot
from app.domain.product_terms import BatchComparisonDraft, BatchComparisonSet
from test_competitor_automatic_limit import AutomaticLimitTests, asin


class CompetitorResilienceTests(unittest.TestCase):
    def fixture(self):
        f = AutomaticLimitTests()
        self.addCleanup(f.doCleanups)
        service = f.make_service({0: "100", 1: "100", 2: "100"})
        return f, service

    def test_good_asins_are_kept_and_failures_are_visible_in_existing_frontend_notices(self):
        f, service = self.fixture()
        original = f.model.compare_product_batch
        def compare(reference, products):
            good = original(reference, products).items[0]
            return BatchComparisonSet(items=[good,
                BatchComparisonDraft(asin=asin(1), valid=False, exclusion_code="PRODUCT_DATA_INCOMPLETE", exclusion_reason="资料不足：规格缺失"),
                BatchComparisonDraft(asin=asin(2), valid=False, exclusion_code="MODEL_OUTPUT_INVALID", exclusion_reason="模型处理失败：缺少规格分数")])
        f.model.compare_product_batch = compare
        result = service.download_poe_all(f.project, f.run, 0)
        self.assertEqual([item.asin for item in result.items], [asin(0)])
        self.assertIsNotNone(result.confirmed_at)
        notices = "\n".join(result.warnings)
        self.assertIn("成功比较 1", notices)
        self.assertIn("资料不足 1", notices)
        self.assertIn("模型处理失败 1", notices)
        self.assertIn(asin(1), notices)
        self.assertIn(asin(2), notices)

    def test_title_only_cached_products_are_refetched_before_comparison(self):
        f, service = self.fixture()
        service._shared_product_snapshots = lambda run: {asin(i): ProductSnapshot(asin=asin(i), title="rack") for i in range(3)}
        captured = []
        def fetch_many(marketplace, asins):
            captured.extend(asins)
            return [(a, ProductSnapshot(asin=a, title="rack", features=["steel frame", "capacity 100 lb"]), None) for a in asins]
        service.products.fetch_many = fetch_many
        result = service.download_poe_all(f.project, f.run, 0)
        self.assertEqual(captured, [asin(0), asin(1), asin(2)])
        self.assertEqual(len(result.items), 3)

    def test_identity_mismatch_is_reported_instead_of_silently_discarded(self):
        f, service = self.fixture()
        def fetch_many(marketplace, asins):
            return [(a, ProductSnapshot(asin=asin(99), title="wrong"), None) for a in asins]
        service.products.fetch_many = fetch_many
        result = service.download_poe_all(f.project, f.run, 0)
        self.assertEqual(result.items, [])
        notices = "\n".join(result.warnings)
        for i in range(3): self.assertIn(asin(i), notices)
        self.assertIsNone(result.confirmed_at)

if __name__ == "__main__":
    unittest.main()
