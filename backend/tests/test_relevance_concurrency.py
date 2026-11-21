"""Focused regressions for concurrent product relevance comparison."""

import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from typing import cast

import httpx
import pytest
from sqlalchemy import Engine

from app.domain.asin_product import ProductSnapshot
from app.domain.errors import DomainError
from app.domain.product_terms import (
    BatchComparisonDraft,
    BatchComparisonSet,
    DimensionScores,
    ProductProfile,
)
from app.services.product_term_analysis import ProductTermAnalysisService
from app.services.qwen import QwenClient, QwenConfig
from test_product_term_analysis import KeywordSearch, build_analysis


def successful_batch(products: list[ProductSnapshot]) -> BatchComparisonSet:
    return BatchComparisonSet(
        items=[
            BatchComparisonDraft(
                asin=product.asin,
                purpose=["支撑大门"],
                style_structure=["夹具与轮子"],
                specifications=["户外"],
                package_quantity=Decimal("1"),
                unit_basis="each",
                scores=DimensionScores(
                    purpose=90, style_structure=90, specifications=90,
                    purpose_reason="用途相同", style_structure_reason="结构相同",
                    specifications_reason="规格相同",
                ),
            )
            for product in products
        ]
    )


class ConcurrentComparisonModel:
    def __init__(self) -> None:
        self.config = QwenConfig(api_key="test")
        self.barrier = threading.Barrier(3)
        self.release_last = threading.Event()
        self.fast_batches_returned = threading.Event()
        self.lock = threading.Lock()
        self.active = 0
        self.peak_active = 0
        self.fast_count = 0

    def compare_product_batch(self, _reference: ProductProfile, products: list[ProductSnapshot]) -> BatchComparisonSet:
        with self.lock:
            self.active += 1
            self.peak_active = max(self.peak_active, self.active)
        try:
            self.barrier.wait(timeout=3)
            if products[0].asin == "B000000021":
                assert self.release_last.wait(timeout=5)
            else:
                with self.lock:
                    self.fast_count += 1
                    if self.fast_count == 2:
                        self.fast_batches_returned.set()
            return successful_batch(products)
        finally:
            with self.lock:
                self.active -= 1


def test_three_batches_run_concurrently_and_each_success_is_saved(
    engine: Engine, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KEYWORD_RELEVANCE_MODEL_CONCURRENCY", "3")
    asins = [f"B{number:09d}" for number in range(1, 31)]
    model = ConcurrentComparisonModel()
    project, run, _candidates, service, _search = build_analysis(
        engine, tmp_path, ["gate wheel"], KeywordSearch({"gate wheel": asins}), cast(QwenClient, model)
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(service.start, project, run)
        try:
            assert model.fast_batches_returned.wait(timeout=4)
            deadline = time.monotonic() + 3
            persisted = 0
            while time.monotonic() < deadline:
                persisted = len(service.get(project, run).items[0].comparisons)
                if persisted >= 20:
                    break
                time.sleep(0.02)
            assert persisted == 20
            assert task.done() is False
        finally:
            model.release_last.set()
        result = task.result(timeout=5)
    assert model.peak_active == 3
    assert [row.asin for row in result.items[0].comparisons] == asins


class FormatFallbackModel:
    def __init__(self) -> None:
        self.config = QwenConfig(api_key="test")
        self.batch_sizes: list[int] = []

    def compare_product_batch(self, _reference: ProductProfile, products: list[ProductSnapshot]) -> BatchComparisonSet:
        self.batch_sizes.append(len(products))
        if len(products) == 10:
            raise DomainError(502, "MODEL_OUTPUT_INVALID", "批次格式不完整")
        return successful_batch(products)


def test_format_failure_splits_ten_into_five_plus_five(engine: Engine, tmp_path) -> None:
    asins = [f"B{number:09d}" for number in range(1, 11)]
    model = FormatFallbackModel()
    project, run, _candidates, service, _search = build_analysis(
        engine, tmp_path, ["gate wheel"], KeywordSearch({"gate wheel": asins}), cast(QwenClient, model)
    )
    result = service.start(project, run)
    assert result.status == "completed"
    assert model.batch_sizes == [10, 5, 5]


class RateLimitedComparisonModel:
    def __init__(self) -> None:
        self.config = QwenConfig(api_key="test")
        self.barrier = threading.Barrier(3)
        self.lock = threading.Lock()
        self.active = 0
        self.calls: Counter[str] = Counter()
        self.retry_active_before: int | None = None

    def compare_product_batch(self, _reference: ProductProfile, products: list[ProductSnapshot]) -> BatchComparisonSet:
        batch = products[0].asin
        with self.lock:
            attempt = self.calls[batch]
            self.calls[batch] += 1
            active_before = self.active
            self.active += 1
            if attempt == 1:
                self.retry_active_before = active_before
        try:
            if attempt == 0:
                self.barrier.wait(timeout=3)
                if batch == "B000000001":
                    raise DomainError(503, "MODEL_PROVIDER_ERROR", "模型限流")
            return successful_batch(products)
        finally:
            with self.lock:
                self.active -= 1


def test_429_batch_is_retried_only_after_parallel_calls_finish(
    engine: Engine, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KEYWORD_RELEVANCE_MODEL_CONCURRENCY", "3")
    asins = [f"B{number:09d}" for number in range(1, 31)]
    model = RateLimitedComparisonModel()
    project, run, _candidates, service, _search = build_analysis(
        engine, tmp_path, ["gate wheel"], KeywordSearch({"gate wheel": asins}), cast(QwenClient, model)
    )
    result = service.start(project, run)
    assert result.status == "completed"
    assert model.calls == Counter({"B000000001": 2, "B000000011": 1, "B000000021": 1})
    assert model.retry_active_before == 0


class ProviderFailureModel:
    def __init__(self) -> None:
        self.config = QwenConfig(api_key="test")
        self.calls = 0

    def compare_product_batch(self, _reference: ProductProfile, _products: list[ProductSnapshot]) -> BatchComparisonSet:
        self.calls += 1
        raise DomainError(502, "MODEL_PROVIDER_ERROR", "模型服务异常")


def test_non_429_provider_failure_is_not_retried(engine: Engine, tmp_path) -> None:
    model = ProviderFailureModel()
    project, run, _candidates, service, _search = build_analysis(
        engine, tmp_path, ["gate wheel"], KeywordSearch({"gate wheel": ["B000000001"]}), cast(QwenClient, model)
    )
    result = service.start(project, run)
    assert result.status == "incomplete"
    assert result.items[0].status == "failed"
    assert model.calls == 1


def test_product_comparison_uses_bounded_output_budget_for_qwen() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(request.content)
        assert body["model"] == "qwen3.7-flash"
        assert body["enable_thinking"] is False
        assert body["max_tokens"] == 8000
        output = {"items": [{
            "asin": "B000000001", "valid": False, "exclusion_reason": "用途不同",
            "purpose": [], "style_structure": [], "specifications": [],
            "package_quantity": None, "unit_basis": None, "scores": None,
        }]}
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": __import__("json").dumps(output)}}]})

    client = QwenClient(QwenConfig(api_key="test", model="qwen3.7-flash"), transport=httpx.MockTransport(handle))
    result = client.compare_product_batch(
        ProductProfile(purpose=["收纳"], style_structure=["支架"], specifications=["家用"]),
        [ProductSnapshot(asin="B000000001", title="Rack")],
    )
    assert result.items[0].asin == "B000000001"
