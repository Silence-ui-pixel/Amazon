"""Per-ASIN recovery must preserve good comparisons without inventing facts."""
import copy
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from app.domain.asin_product import ProductSnapshot
from app.domain.errors import DomainError
from app.domain.product_terms import ProductProfile
from app.services.qwen import QwenClient, QwenConfig

A = "B000000001"
B = "B000000002"

def row(asin):
    return {"asin": asin, "valid": True, "purpose": ["storage"],
            "style_structure": ["rack"], "specifications": ["steel"],
            "scores": {"purpose": 80, "style_structure": 80, "specifications": 80,
                       "purpose_reason": "purpose evidence", "style_structure_reason": "structure evidence",
                       "specifications_reason": "spec evidence"}}

class ComparisonResilienceTests(unittest.TestCase):
    def compare(self, responses, products=None, diagnostic_dir=None):
        self.sent = []
        responses = iter(responses)
        def handle(request):
            text = json.loads(request.content)["messages"][0]["content"][0]["text"]
            data = json.loads(text.split("。数据：", 1)[1])
            self.sent.append([p["asin"] for p in data["products"]])
            value = next(responses)
            if isinstance(value, Exception):
                raise value
            raw = value if isinstance(value, str) else json.dumps(value)
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": raw}}]})
        client = QwenClient(QwenConfig(api_key="test-only"), transport=httpx.MockTransport(handle), diagnostic_dir=diagnostic_dir)
        reference = ProductProfile(purpose=["storage"], style_structure=["rack"], specifications=["steel"])
        return client.compare_product_batch(reference, products or [ProductSnapshot(asin=A, title="rack"), ProductSnapshot(asin=B, title="rack")])

    def test_missing_specifications_excludes_only_that_asin_without_retry(self):
        bad = row(B); bad["specifications"] = []
        result = self.compare([{"items": [row(A), bad]}])
        self.assertTrue(result.items[0].valid)
        self.assertFalse(result.items[1].valid)
        self.assertEqual(result.items[1].exclusion_code, "PRODUCT_DATA_INCOMPLETE")
        self.assertIn("规格", result.items[1].exclusion_reason)
        self.assertIsNone(result.items[1].scores)
        self.assertEqual(self.sent, [[A, B]])

    def test_bad_scores_retry_only_bad_asin_and_keep_original_good_scores(self):
        bad = row(B); bad["scores"].pop("specifications")
        result = self.compare([{"items": [row(A), bad]}, {"items": [row(B)]}])
        self.assertEqual(self.sent, [[A, B], [B]])
        self.assertTrue(all(item.valid for item in result.items))
        self.assertEqual(result.items[0].scores.purpose, 80)

    def test_bad_extra_field_retries_once_then_records_failure(self):
        bad = row(B); bad["exuation_reason"] = "typo"
        with TemporaryDirectory() as directory:
            result = self.compare([{"items": [row(A), bad]}, {"items": [bad]}], diagnostic_dir=Path(directory))
            self.assertTrue(result.items[0].valid)
            self.assertFalse(result.items[1].valid)
            self.assertEqual(result.items[1].exclusion_code, "MODEL_OUTPUT_INVALID")
            self.assertEqual(self.sent, [[A, B], [B]])
            diagnostics = json.loads(next(Path(directory).glob("product-compare-*.json")).read_text())
            self.assertTrue(diagnostics["item_issues"])
            self.assertEqual(len(diagnostics["retries"]), 1)

    def test_out_of_order_and_unrequested_row_do_not_replace_requested_products(self):
        result = self.compare([{"items": [row(B), row("B000000003"), row(A)]}])
        self.assertEqual([item.asin for item in result.items], [A, B])
        self.assertEqual(self.sent, [[A, B]])

    def test_duplicate_asin_retries_only_ambiguous_product(self):
        result = self.compare([{"items": [row(A), row(B), row(B)]}, {"items": [row(B)]}])
        self.assertEqual([item.asin for item in result.items], [A, B])
        self.assertEqual(self.sent, [[A, B], [B]])

    def test_missing_asin_retries_only_missing_product(self):
        result = self.compare([{"items": [row(A)]}, {"items": [row(B)]}])
        self.assertTrue(result.items[1].valid)
        self.assertEqual(self.sent, [[A, B], [B]])

    def test_whitespace_facts_cannot_be_treated_as_complete(self):
        bad = row(B); bad["specifications"] = ["  ", ""]
        result = self.compare([{"items": [row(A), bad]}])
        self.assertFalse(result.items[1].valid)
        self.assertEqual(result.items[1].exclusion_code, "PRODUCT_DATA_INCOMPLETE")

    def test_initial_timeout_remains_unknown_and_is_not_retried(self):
        with self.assertRaises(DomainError) as caught:
            self.compare([httpx.ReadTimeout("private error")])
        self.assertEqual(caught.exception.code, "MODEL_OUTCOME_UNKNOWN")
        self.assertEqual(self.sent, [[A, B]])

    def test_retry_timeout_keeps_good_asin_without_retrying_unknown_call(self):
        bad = row(B); bad["scores"] = None
        result = self.compare([{"items": [row(A), bad]}, httpx.ReadTimeout("private error")])
        self.assertTrue(result.items[0].valid)
        self.assertFalse(result.items[1].valid)
        self.assertEqual(result.items[1].exclusion_code, "MODEL_OUTCOME_UNKNOWN")
        self.assertEqual(self.sent, [[A, B], [B]])
        self.assertNotIn("private", result.items[1].exclusion_reason)

    def test_invalid_json_retries_each_requested_asin_once(self):
        result = self.compare(["{", {"items": [row(A)]}, {"items": [row(B)]}])
        self.assertTrue(all(item.valid for item in result.items))
        self.assertEqual(self.sent, [[A, B], [A], [B]])

    def test_explicit_data_shortage_is_classified_without_misclassifying_other_exclusions(self):
        for reason, facts, code, expected in [
            ("规格资料不足", {}, "", "PRODUCT_DATA_INCOMPLETE"),
            ("insufficient specification information", {}, "", "PRODUCT_DATA_INCOMPLETE"),
            ("不同用途", {}, "", ""),
            ("规格资料不足", {}, "CUSTOM_EXCLUSION", "CUSTOM_EXCLUSION"),
            ("missing optional package quantity", {"purpose": ["storage"], "style_structure": ["rack"], "specifications": ["steel"]}, "", ""),
        ]:
            with self.subTest(reason=reason, code=code):
                bad = {"asin": B, "valid": False, "exclusion_reason": reason, "exclusion_code": code, **facts}
                result = self.compare([{"items": [row(A), bad]}])
                self.assertEqual(result.items[1].exclusion_code, expected)
                self.assertEqual(self.sent, [[A, B]])

    def test_explicit_unrelated_product_is_not_retried(self):
        result = self.compare([{"items": [row(A), {"asin": B, "valid": False, "exclusion_reason": "不同用途"}]}])
        self.assertFalse(result.items[1].valid)
        self.assertEqual(self.sent, [[A, B]])

if __name__ == "__main__":
    unittest.main()
