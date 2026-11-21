"""Cross-step snapshot reuse and bounded concurrency for hot-search core analysis."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast
from uuid import uuid4

from sqlalchemy import Engine

from app.domain.asin_product import ProductSnapshot
from app.domain.errors import DomainError
from app.domain.product_terms import ProductTermAnalysis, TermAnalysisItem
from app.domain.verification import CollectionResult
from app.services.hot_search_core_analysis import HotSearchCoreAnalysisService
from app.services.qwen import QwenClient
from test_hot_search_core_analysis import Search, execute, setup_analysis


def _store_first_step_snapshot(
    service: HotSearchCoreAnalysisService,
    project,
    run,
    snapshot: ProductSnapshot,
) -> None:
    analysis = ProductTermAnalysis(
        id=uuid4(),
        project_id=project,
        run_id=run,
        profile_version=1,
        status="completed",
        complete=True,
        items=[
            TermAnalysisItem(
                candidate_id=uuid4(),
                text="shoe rack",
                status="completed",
                collection=CollectionResult(
                    term="shoe rack",
                    raw_natural_count=1,
                    unique_asins=[snapshot.asin],
                    products=[snapshot],
                    pages=[],
                ),
            )
        ],
    )
    with service.repository.connect() as conn:
        conn.execute(
            "INSERT INTO product_term_analyses VALUES(?,?,?,?)",
            (str(analysis.id), str(run), uuid4().hex, analysis.model_dump_json()),
        )


def test_fresh_first_step_snapshot_skips_second_step_detail_request(
    db: str, engine: Engine, tmp_path
) -> None:
    service, project, run, report = setup_analysis(db, engine, tmp_path, count=1)
    _store_first_step_snapshot(
        service,
        project,
        run,
        ProductSnapshot(
            asin="B000000001",
            title="cached shoe rack",
            price=Decimal("20"),
            currency="USD",
            captured_at=datetime.now(UTC),
        ),
    )

    result = execute(service, project, run, report)

    assert cast(Search, service.search).seen == set()
    assert result.snapshots["B000000001"].title == "cached shoe rack"


def test_expired_first_step_snapshot_is_refetched(
    db: str, engine: Engine, tmp_path
) -> None:
    service, project, run, report = setup_analysis(db, engine, tmp_path, count=1)
    _store_first_step_snapshot(
        service,
        project,
        run,
        ProductSnapshot(
            asin="B000000001",
            title="expired shoe rack",
            price=Decimal("20"),
            currency="USD",
            captured_at=datetime.now(UTC) - timedelta(hours=25),
        ),
    )

    result = execute(service, project, run, report)

    assert cast(Search, service.search).seen == {"B000000001"}
    assert result.snapshots["B000000001"].title == "shoe rack"


class BatchSearch(Search):
    def __init__(self, repository, count: int) -> None:
        super().__init__(repository, count)
        self.batch_calls: list[list[str]] = []
        self.single_calls: list[str] = []

    def product(self, asin: str) -> ProductSnapshot:
        self.single_calls.append(asin)
        return self._snapshot(asin)

    def products(self, asins: list[str]):
        self.batch_calls.append(list(asins))
        return [(asin, self._snapshot(asin), None) for asin in asins]

    def _snapshot(self, asin: str) -> ProductSnapshot:
        return ProductSnapshot(
            asin=asin,
            title="shoe rack",
            price=Decimal("20"),
            currency="USD",
            price_reliable=True,
        )


def test_second_step_uses_one_batch_detail_request_for_missing_asins(
    db: str, engine: Engine, tmp_path
) -> None:
    service, project, run, report = setup_analysis(db, engine, tmp_path, count=30)
    batch_search = BatchSearch(service.repository, 30)
    service.search = batch_search

    result = execute(service, project, run, report)

    assert result.status == "completed"
    assert batch_search.single_calls == []
    assert len(batch_search.batch_calls) == 1
    assert len(batch_search.batch_calls[0]) == 30


class BlockingComparisonModel:
    def __init__(self, wrapped: QwenClient) -> None:
        self.wrapped = wrapped
        self.config = wrapped.config
        self.barrier = threading.Barrier(3)
        self.release_last = threading.Event()
        self.fast_batches_returned = threading.Event()
        self.lock = threading.Lock()
        self.active = 0
        self.peak_active = 0
        self.fast_count = 0

    def classify_hot_search_intents(self, *args, **kwargs):
        return self.wrapped.classify_hot_search_intents(*args, **kwargs)

    def compare_product_batch(self, reference, products):
        with self.lock:
            self.active += 1
            self.peak_active = max(self.peak_active, self.active)
        try:
            self.barrier.wait(timeout=4)
            if products[0].asin == "B000000021":
                assert self.release_last.wait(timeout=6)
            result = self.wrapped.compare_product_batch(reference, products)
            if products[0].asin != "B000000021":
                with self.lock:
                    self.fast_count += 1
                    if self.fast_count == 2:
                        self.fast_batches_returned.set()
            return result
        finally:
            with self.lock:
                self.active -= 1


def test_three_model_batches_run_concurrently_and_save_each_success(
    db: str, engine: Engine, tmp_path
) -> None:
    service, project, run, report = setup_analysis(db, engine, tmp_path, count=30)
    assert service.client is not None
    model = BlockingComparisonModel(service.client)
    service.client = cast(QwenClient, model)
    queued = service.start(project, run, report.id, [])

    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(service.execute, project, run, queued.id)
        try:
            assert model.fast_batches_returned.wait(timeout=5)
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
        result = task.result(timeout=8)

    assert model.peak_active == 3
    assert [row.natural_position for row in result.items[0].comparisons] == list(range(1, 31))


class FormatFallbackModel:
    def __init__(self, wrapped: QwenClient) -> None:
        self.wrapped = wrapped
        self.config = wrapped.config
        self.batch_sizes: list[int] = []

    def classify_hot_search_intents(self, *args, **kwargs):
        return self.wrapped.classify_hot_search_intents(*args, **kwargs)

    def compare_product_batch(self, reference, products):
        self.batch_sizes.append(len(products))
        if len(products) == 10:
            raise DomainError(502, "MODEL_OUTPUT_INVALID", "批次格式不完整")
        return self.wrapped.compare_product_batch(reference, products)


def test_invalid_ten_product_response_splits_into_five_plus_five(
    db: str, engine: Engine, tmp_path
) -> None:
    service, project, run, report = setup_analysis(db, engine, tmp_path, count=10)
    assert service.client is not None
    model = FormatFallbackModel(service.client)
    service.client = cast(QwenClient, model)

    result = execute(service, project, run, report)

    assert result.status == "completed"
    assert model.batch_sizes == [10, 5, 5]


class RateLimitedModel:
    def __init__(self, wrapped: QwenClient) -> None:
        self.wrapped = wrapped
        self.config = wrapped.config
        self.barrier = threading.Barrier(3)
        self.lock = threading.Lock()
        self.active = 0
        self.calls: dict[str, int] = {}
        self.retry_active_before: int | None = None

    def classify_hot_search_intents(self, *args, **kwargs):
        return self.wrapped.classify_hot_search_intents(*args, **kwargs)

    def compare_product_batch(self, reference, products):
        key = products[0].asin
        with self.lock:
            attempt = self.calls.get(key, 0)
            self.calls[key] = attempt + 1
            active_before = self.active
            self.active += 1
            if attempt == 1:
                self.retry_active_before = active_before
        try:
            if attempt == 0:
                self.barrier.wait(timeout=4)
                if key == "B000000001":
                    raise DomainError(503, "MODEL_PROVIDER_ERROR", "模型限流")
            return self.wrapped.compare_product_batch(reference, products)
        finally:
            with self.lock:
                self.active -= 1


def test_429_batch_retries_serially_after_parallel_calls_finish(
    db: str, engine: Engine, tmp_path
) -> None:
    service, project, run, report = setup_analysis(db, engine, tmp_path, count=30)
    assert service.client is not None
    model = RateLimitedModel(service.client)
    service.client = cast(QwenClient, model)

    result = execute(service, project, run, report)

    assert result.status == "completed"
    assert model.calls == {"B000000001": 2, "B000000011": 1, "B000000021": 1}
    assert model.retry_active_before == 0
