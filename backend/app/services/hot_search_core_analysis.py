"""All hot-search terms, durable call claims, and existing four-dimension scoring."""

import hashlib
import json
import os
from _thread import LockType
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextlib import AbstractContextManager, nullcontext
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import partial
from threading import BoundedSemaphore, Lock
from time import monotonic
from typing import Literal, TypeVar, cast
from uuid import UUID, uuid4, uuid5

from pydantic import BaseModel, Field

from app.adapters.remote_browser import submit_with_browser_operation_context
from app.db.repository import Repository
from app.domain.asin_product import ProductSnapshot
from app.domain.core_terms import Brand, HotSearchCoreItem
from app.domain.errors import DomainError
from app.domain.product_terms import (
    BatchComparisonDraft,
    BatchComparisonSet,
    ProductComparison,
    ProductProfile,
    ProductTermAnalysis,
)
from app.domain.roots import RootCandidates, RootSelection
from app.domain.schemas import Contract
from app.domain.verification import CollectionResult, SearchPage
from app.services.amazon_search import (
    SearchProvider,
    SearchSession,
    require_us_delivery,
)
from app.services.core_terms import CoreService
from app.services.hot_search_core_rules import is_core_eligible, sort_core_items
from app.services.product_profiles import ProductProfileService
from app.services.product_term_collection import (
    PRODUCT_RETRY_CODES,
    ProductSearchSession,
    collect_natural_products,
)
from app.services.product_term_scoring import score_product, summarize_term
from app.services.qwen import QwenClient, QwenConfig
from app.services.product_comparison_semantics import COMPARISON_VERSION
from app.services.search_policy import PAUSE_CODES

T = TypeVar("T")
_JOURNAL_MISSING = object()
RULE_VERSION = "hot-search-core-v2"
COLLECTION_VERSION = "product-read-thread-v4-shared-cache"
SHARED_SNAPSHOT_MAX_AGE = timedelta(hours=24)
BatchKind = Literal["open", "intent", "collection", "search", "product", "comparison"]


def _model_concurrency() -> int:
    try:
        configured = int(os.getenv("KEYWORD_CORE_MODEL_CONCURRENCY", "12") or "12")
    except ValueError:
        configured = 12
    return max(1, min(12, configured))


def _term_concurrency(search: SearchProvider) -> int:
    if not getattr(search, "supports_parallel_terms", False):
        return 1
    try:
        configured = int(os.getenv("KEYWORD_CORE_TERM_CONCURRENCY", "4") or "4")
    except ValueError:
        configured = 4
    return max(1, min(4, configured))


class HotSearchCoreAnalysisRequest(Contract):
    report_id: UUID
    own_brands: list[Brand] = Field(default_factory=list[Brand], max_length=20)
    retry_incomplete: bool = False
    retry_unknown_model_outcomes: bool = False
    force_reanalysis: bool = False


class AnalysisBatch(Contract):
    id: UUID = Field(default_factory=uuid4)
    kind: BatchKind
    candidate_ids: list[UUID] = Field(default_factory=list[UUID])
    request: dict[str, object] = Field(default_factory=dict)
    attempt: int = 1
    status: Literal[
        "planned", "started", "completed", "invalid", "failed", "outcome_unknown"
    ] = "planned"
    result: dict[str, object] | None = None
    error_code: str | None = None
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None


class HotSearchCoreAnalysis(Contract):
    id: UUID = Field(default_factory=uuid4)
    retry_of: UUID | None = None
    project_id: UUID
    run_id: UUID
    report_id: UUID
    fingerprint: str
    rule_version: Literal["hot-search-core-v1", "hot-search-core-v2"] = RULE_VERSION
    profile_version: int
    profile_snapshot: ProductProfile
    own_brands_snapshot: list[str]
    marketplace: str
    delivery_region: str
    input_snapshot: dict[str, object]
    status: Literal[
        "queued", "running", "completed", "incomplete", "failed", "stale"
    ] = "queued"
    complete: bool = False
    stale: bool = False
    source_partial: bool = False
    items: list[HotSearchCoreItem]
    recommended_candidate_ids: list[UUID] = Field(default_factory=list[UUID])
    batches: list[AnalysisBatch] = Field(default_factory=list[AnalysisBatch])
    replayed_unknown_batches: list[AnalysisBatch] = Field(
        default_factory=list[AnalysisBatch]
    )
    collections: dict[str, CollectionResult] = Field(default_factory=dict)
    snapshots: dict[str, ProductSnapshot] = Field(default_factory=dict)
    snapshot_failures: dict[str, str] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def _fingerprint(snapshot: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def _journal_safe_value(value: object) -> object:
    if isinstance(value, BaseModel):
        return {"$type": type(value).__name__, "value": value.model_dump(mode="json")}
    if isinstance(value, dict):
        return {str(key): _journal_safe_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_journal_safe_value(item) for item in value]
    if isinstance(value, Exception):
        return {"$error": str(value)[:500], "$type": type(value).__name__}
    if isinstance(value, (UUID, datetime, Decimal)):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return {"$type": type(value).__name__}


def _completed_document_for_fingerprint(
    documents: list[str], fingerprint: str
) -> str | None:
    """Prefer a successful salted retry over the original incomplete fingerprint row."""
    for document in documents:
        try:
            analysis = HotSearchCoreAnalysis.model_validate_json(document)
        except ValueError:
            continue
        if (
            analysis.fingerprint == fingerprint
            and analysis.status == "completed"
            and analysis.complete
            and not analysis.stale
        ):
            return document
    return None


def _compatible_comparison_refresh(
    previous: dict[str, object], current: dict[str, object]
) -> bool:
    """Only a comparison revision may differ; all source and business inputs must match."""
    return previous.get("comparison_version") != current.get("comparison_version") and {
        key: value for key, value in previous.items() if key != "comparison_version"
    } == {key: value for key, value in current.items() if key != "comparison_version"}


def _comparison_failure_codes(batch: AnalysisBatch) -> set[str]:
    if batch.kind != "comparison" or batch.result is None:
        return set()
    result = BatchComparisonSet.model_validate(batch.result)
    return {
        row.exclusion_code
        for row in result.items
        if row.exclusion_code.startswith("MODEL_")
        or row.exclusion_code == "OUTCOME_UNKNOWN"
    }


_NONFATAL_COLLECTION_CODES = frozenset({
    "ASIN_IDENTITY_UNCONFIRMED",
    "PRODUCT_DETAIL_ALREADY_ATTEMPTED",
})


def _collection_failures_are_nonfatal(
    item: HotSearchCoreItem, collection: CollectionResult
) -> bool:
    if not collection.failures:
        return True
    return (
        all(
            failure.error_code in _NONFATAL_COLLECTION_CODES
            for failure in collection.failures
        )
        and item.status == "completed"
        and item.summary is not None
        and item.summary.confidence == "high"
        and item.summary.valid_samples >= 20
    )


def _collection_snapshot_is_complete(collection: CollectionResult) -> bool:
    requested = collection.unique_asins
    product_asins = [product.asin for product in collection.products]
    failure_asins = [failure.asin for failure in collection.failures]
    products = set(product_asins)
    failures = set(failure_asins)
    return (
        len(requested) == len(set(requested))
        and len(product_asins) == len(products)
        and len(failure_asins) == len(failures)
        and products.isdisjoint(failures)
        and products | failures == set(requested)
    )


class HotSearchCoreAnalysisService:
    def __init__(
        self,
        repository: Repository,
        core: CoreService,
        profiles: ProductProfileService,
        search: SearchProvider,
        client: QwenClient | None,
    ) -> None:
        self.repository = repository
        self.core = core
        self.profiles = profiles
        self.search = search
        self.client = client

    def _input_snapshot(
        self,
        project: UUID,
        run: UUID,
        report_id: UUID,
        own_brands: list[str],
    ) -> dict[str, object]:
        pipeline = self.repository.get_run(project, run)
        profile = self.profiles.get(project, run)
        if profile.status != "confirmed":
            raise DomainError(409, "PRODUCT_PROFILE_REQUIRED", "请先确认商品四维基准")
        report = self.core.aba.get(project, run, report_id)
        if report.stale:
            raise DomainError(
                409, "CORE_REPORT_STALE", "商品词或商品资料已变化，请重新采集周报"
            )
        with self.repository.connect() as conn:
            selection = conn.execute(
                "SELECT document FROM root_selections WHERE run_id=?", (str(run),)
            ).fetchone()
            candidates = conn.execute(
                "SELECT document FROM root_candidates WHERE run_id=?", (str(run),)
            ).fetchone()
        if not selection or not candidates:
            raise DomainError(409, "ROOT_SELECTION_REQUIRED", "请先确认商品词选择")
        selected = RootSelection.model_validate_json(selection[0])
        options = RootCandidates.model_validate_json(candidates[0])
        mapping = {item.id: item.text for item in options.candidates}
        if (
            options.rule_version != "product-term-v1"
            or not selected.candidate_ids
            or any(
                candidate_id not in mapping for candidate_id in selected.candidate_ids
            )
        ):
            raise DomainError(
                409, "PRODUCT_TERM_CANDIDATES_REQUIRED", "需要有效的新规则商品词选择"
            )
        return {
            "selection": [
                {"id": str(key), "text": mapping[key]}
                for key in sorted(selected.candidate_ids)
            ],
            "profile_version": profile.version,
            "profile": profile.profile.model_dump(mode="json"),
            "report": report.model_dump(mode="json"),
            "own_brands": own_brands,
            "marketplace": pipeline.input.marketplace,
            "delivery_region": os.getenv("KEYWORD_US_DELIVERY_ZIP", "10001").strip(),
            "rule_version": RULE_VERSION,
            "collection_version": COLLECTION_VERSION,
            "comparison_version": COMPARISON_VERSION,
        }

    def _load(
        self, project: UUID, run: UUID, analysis_id: UUID | None = None
    ) -> HotSearchCoreAnalysis:
        self.repository.get_run(project, run)
        with self.repository.connect() as conn:
            row = conn.execute(
                "SELECT document FROM hot_search_core_analyses WHERE run_id=? "
                + (
                    "AND id=?"
                    if analysis_id
                    else "AND id=(SELECT analysis_id FROM hot_search_core_current WHERE run_id="
                    "hot_search_core_analyses.run_id)"
                ),
                (str(run), str(analysis_id)) if analysis_id else (str(run),),
            ).fetchone()
        if not row:
            raise DomainError(
                404, "HOT_SEARCH_CORE_ANALYSIS_NOT_FOUND", "尚无热门搜索词相关度分析"
            )
        return HotSearchCoreAnalysis.model_validate_json(row[0])

    def _view(self, analysis: HotSearchCoreAnalysis) -> HotSearchCoreAnalysis:
        # Staleness is a read projection: immutable historical evidence is never rewritten by GET.
        try:
            snapshot = self._input_snapshot(
                analysis.project_id,
                analysis.run_id,
                analysis.report_id,
                analysis.own_brands_snapshot,
            )
            analysis.stale = _fingerprint(snapshot) != analysis.fingerprint
        except DomainError:
            analysis.stale = True
        if analysis.stale:
            analysis.status = "stale"
            analysis.complete = False
        return analysis

    def get(
        self, project: UUID, run: UUID, analysis_id: UUID | None = None
    ) -> HotSearchCoreAnalysis:
        analysis = self._load(project, run, analysis_id)
        self._merge_journal(analysis)
        return self._view(analysis)

    def _save(
        self,
        analysis: HotSearchCoreAnalysis,
        *,
        checkpoint_candidate_id: UUID | None = None,
        checkpoint_kind: str | None = None,
        clear_journal: bool = False,
    ) -> None:
        if checkpoint_candidate_id is not None:
            candidate_key = str(checkpoint_candidate_id)
            item = next(
                (
                    current
                    for current in analysis.items
                    if str(current.candidate_id) == candidate_key
                ),
                None,
            )
            if item is None:
                raise ValueError(f"checkpoint candidate is missing: {candidate_key}")
            collection = analysis.collections.get(candidate_key)
            asins = set(collection.unique_asins) if collection is not None else set()
            checkpoint_id = uuid5(analysis.id, f"candidate-checkpoint:{candidate_key}")
            with self.repository.connect() as conn:
                rows = conn.execute(
                    "SELECT batch_id,kind,document,seq "
                    "FROM hot_search_core_analysis_journal "
                    "WHERE analysis_id=? AND candidate_id=? ORDER BY seq",
                    (str(analysis.id), candidate_key),
                ).fetchall()
                batch_sequences: dict[str, int] = {}
                for batch_id, kind, saved_document, seq in rows:
                    if kind == "checkpoint":
                        saved_payload = json.loads(cast(str, saved_document))
                        saved_checkpoint = saved_payload.get("checkpoint")
                        if isinstance(saved_checkpoint, dict):
                            for saved_batch in cast(
                                list[dict[str, object]],
                                saved_checkpoint.get("batches", []),
                            ):
                                batch_data = saved_batch.get("batch")
                                if isinstance(batch_data, dict):
                                    batch_sequences[str(batch_data["id"])] = int(
                                        saved_batch["seq"]
                                    )
                    else:
                        batch_sequences[str(batch_id)] = int(seq)
                batch_history = [
                    {
                        "seq": batch_sequences.get(
                            str(batch.id), position - len(analysis.batches) - 1
                        ),
                        "batch": batch.model_dump(mode="json"),
                    }
                    for position, batch in enumerate(analysis.batches)
                    if batch.candidate_ids == [item.candidate_id]
                ]
                checkpoint = {
                    "candidate_id": candidate_key,
                    "item": item.model_dump(mode="json"),
                    "batches": batch_history,
                    "collection": (
                        collection.model_dump(mode="json")
                        if collection is not None
                        else None
                    ),
                    "snapshots": {
                        asin: snapshot.model_dump(mode="json")
                        for asin, snapshot in analysis.snapshots.items()
                        if asin in asins
                    },
                    "snapshot_failures": {
                        asin: error
                        for asin, error in analysis.snapshot_failures.items()
                        if asin in asins
                    },
                }
                document = json.dumps(
                    {"checkpoint": checkpoint},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                conn.execute(
                    "INSERT INTO hot_search_core_analysis_journal "
                    "(analysis_id,batch_id,candidate_id,kind,document) VALUES(?,?,?,?,?) "
                    "ON CONFLICT (analysis_id,batch_id) DO UPDATE SET "
                    "candidate_id=excluded.candidate_id,kind=excluded.kind,document=excluded.document",
                    (
                        str(analysis.id),
                        str(checkpoint_id),
                        candidate_key,
                        "checkpoint",
                        document,
                    ),
                )
                conn.execute(
                    "DELETE FROM hot_search_core_analysis_journal "
                    "WHERE analysis_id=? AND candidate_id=? AND batch_id<>?",
                    (str(analysis.id), candidate_key, str(checkpoint_id)),
                )
            return
        with self.repository.connect() as conn:
            conn.execute(
                "UPDATE hot_search_core_analyses SET document=? WHERE id=?",
                (analysis.model_dump_json(), str(analysis.id)),
            )
            if clear_journal:
                conn.execute(
                    "DELETE FROM hot_search_core_analysis_journal WHERE analysis_id=?",
                    (str(analysis.id),),
                )
            else:
                if checkpoint_candidate_id is not None:
                    conn.execute(
                        "DELETE FROM hot_search_core_analysis_journal "
                        "WHERE analysis_id=? AND candidate_id=?",
                        (str(analysis.id), str(checkpoint_candidate_id)),
                    )
                if checkpoint_kind is not None:
                    conn.execute(
                        "DELETE FROM hot_search_core_analysis_journal "
                        "WHERE analysis_id=? AND kind=?",
                        (str(analysis.id), checkpoint_kind),
                    )

    def _save_journal(
        self,
        analysis: HotSearchCoreAnalysis,
        batch: AnalysisBatch,
        *,
        return_value: object = _JOURNAL_MISSING,
    ) -> None:
        payload: dict[str, object] = {"batch": batch.model_dump(mode="json")}
        if return_value is not _JOURNAL_MISSING:
            payload["return_value"] = _journal_safe_value(return_value)
        candidate_id = (
            str(batch.candidate_ids[0]) if len(batch.candidate_ids) == 1 else None
        )
        document = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self.repository.connect() as conn:
            conn.execute(
                "INSERT INTO hot_search_core_analysis_journal "
                "(analysis_id,batch_id,candidate_id,kind,document) VALUES(?,?,?,?,?) "
                "ON CONFLICT (analysis_id,batch_id) DO UPDATE SET "
                "candidate_id=excluded.candidate_id,kind=excluded.kind,document=excluded.document",
                (
                    str(analysis.id),
                    str(batch.id),
                    candidate_id,
                    batch.kind,
                    document,
                ),
            )

    def _merge_journal(self, analysis: HotSearchCoreAnalysis) -> None:
        with self.repository.connect() as conn:
            rows = conn.execute(
                "SELECT document,seq FROM hot_search_core_analysis_journal "
                "WHERE analysis_id=? ORDER BY seq",
                (str(analysis.id),),
            ).fetchall()
        batches = {str(batch.id): index for index, batch in enumerate(analysis.batches)}
        item_indexes = {
            str(item.candidate_id): index for index, item in enumerate(analysis.items)
        }
        items = {key: analysis.items[index] for key, index in item_indexes.items()}
        journal_batches: list[tuple[int, int, AnalysisBatch]] = []
        sequence = 0
        for document, row_sequence in rows:
            payload = json.loads(cast(str, document))
            checkpoint_data = payload.get("checkpoint")
            if isinstance(checkpoint_data, dict):
                candidate_id = str(checkpoint_data["candidate_id"])
                item = HotSearchCoreItem.model_validate(checkpoint_data["item"])
                item_index = item_indexes.get(candidate_id)
                if item_index is not None:
                    analysis.items[item_index] = item
                items[candidate_id] = item
                collection_data = checkpoint_data.get("collection")
                if isinstance(collection_data, dict):
                    analysis.collections[candidate_id] = (
                        CollectionResult.model_validate(collection_data)
                    )
                else:
                    analysis.collections.pop(candidate_id, None)
                for asin, snapshot_data in cast(
                    dict[str, dict[str, object]],
                    checkpoint_data.get("snapshots", {}),
                ).items():
                    analysis.snapshots[asin] = ProductSnapshot.model_validate(
                        snapshot_data
                    )
                for asin, error in cast(
                    dict[str, str], checkpoint_data.get("snapshot_failures", {})
                ).items():
                    analysis.snapshot_failures[asin] = error
                for saved_batch in cast(
                    list[dict[str, object]], checkpoint_data.get("batches", [])
                ):
                    batch_data = saved_batch.get("batch")
                    if isinstance(batch_data, dict):
                        journal_batches.append(
                            (
                                int(saved_batch["seq"]),
                                sequence,
                                AnalysisBatch.model_validate(batch_data),
                            )
                        )
                        sequence += 1
                continue
            batch_data = cast(dict[str, object], payload.get("batch", payload))
            batch = AnalysisBatch.model_validate(batch_data)
            return_value = payload.get("return_value", _JOURNAL_MISSING)
            if batch.result is None and return_value is not _JOURNAL_MISSING:
                if isinstance(return_value, dict) and isinstance(
                    return_value.get("value"), dict
                ):
                    batch.result = cast(dict[str, object], return_value["value"])
                elif isinstance(return_value, dict):
                    batch.result = cast(dict[str, object], return_value)
                else:
                    batch.result = {"recovery_payload": cast(object, return_value)}
            journal_batches.append((int(row_sequence), sequence, batch))
            sequence += 1
        for _, _, batch in sorted(journal_batches, key=lambda row: (row[0], row[1])):
            existing = batches.get(str(batch.id))
            if existing is None:
                batches[str(batch.id)] = len(analysis.batches)
                analysis.batches.append(batch)
            else:
                analysis.batches[existing] = batch
            if batch.status in ("started", "completed"):
                for candidate_id in batch.candidate_ids:
                    item = items.get(str(candidate_id))
                    if item is not None and item.status == "waiting":
                        item.status = "running"

    def _shared_product_snapshots(
        self, run: UUID, *, now: datetime | None = None
    ) -> dict[str, ProductSnapshot]:
        """Reuse only fresh raw detail evidence from step one; search rank stays query-specific."""
        cutoff = (now or datetime.now(UTC)) - SHARED_SNAPSHOT_MAX_AGE
        with self.repository.connect() as conn:
            rows = conn.execute(
                "SELECT document FROM product_term_analyses WHERE run_id=? ORDER BY seq DESC",
                (str(run),),
            ).fetchall()
        shared: dict[str, ProductSnapshot] = {}
        for (document,) in rows:
            try:
                previous = ProductTermAnalysis.model_validate_json(document)
            except ValueError:
                continue
            for item in previous.items:
                if item.collection is None:
                    continue
                for snapshot in item.collection.products:
                    if snapshot.captured_at >= cutoff:
                        shared.setdefault(snapshot.asin, snapshot)
        return shared

    def _reuse_completed_comparisons(
        self,
        analysis: HotSearchCoreAnalysis,
        item: HotSearchCoreItem,
        products: list[ProductSnapshot],
        previous: list[AnalysisBatch],
    ) -> tuple[dict[str, BatchComparisonDraft], list[dict[str, str]]]:
        """Reuse successful rows only when their original collected snapshot is identical."""
        requested = {product.asin: product for product in products}
        finished = {
            source.candidate_id: source
            for source in analysis.items
            if source.candidate_id != item.candidate_id
            and source.status in ("completed", "no_samples")
        }
        reused: dict[str, BatchComparisonDraft] = {}
        sources: list[dict[str, str]] = []
        for saved in previous:
            if (
                saved.kind != "comparison"
                or saved.status != "completed"
                or saved.request.get("comparison_version") != COMPARISON_VERSION
                or len(saved.candidate_ids) != 1
                or saved.result is None
            ):
                continue
            source_id = saved.candidate_ids[0]
            source = finished.get(source_id)
            collection = analysis.collections.get(str(source_id))
            if (
                source is None
                or collection is None
                or not _collection_failures_are_nonfatal(source, collection)
            ):
                continue
            snapshots = {product.asin: product for product in collection.products}
            valid_asins = {
                comparison.asin
                for comparison in source.comparisons
                if comparison.status in ("high_related", "low_related")
                and comparison.candidate_profile is not None
            }
            rows = BatchComparisonSet.model_validate(saved.result)
            if [row.asin for row in rows.items] != saved.request.get("asins"):
                continue
            for row in rows.items:
                snapshot = requested.get(row.asin)
                if (
                    snapshot is None
                    or row.asin in reused
                    or row.asin not in valid_asins
                    or not row.valid
                    or row.exclusion_code
                    or row.scores is None
                    or snapshots.get(row.asin) != snapshot
                ):
                    continue
                reused[row.asin] = row.model_copy(deep=True)
                sources.append(
                    {
                        "asin": row.asin,
                        "candidate_id": str(source_id),
                        "batch_id": str(saved.id),
                        "snapshot_sha256": _fingerprint(
                            snapshot.model_dump(mode="json")
                        ),
                    }
                )
        return reused, sources

    def _compare_once(
        self,
        analysis: HotSearchCoreAnalysis,
        item: HotSearchCoreItem,
        products: list[ProductSnapshot],
        model: QwenClient,
        lock: LockType,
        model_semaphore: BoundedSemaphore | None = None,
    ) -> BatchComparisonSet:
        requested = products
        cached: dict[str, BatchComparisonDraft] = {}
        with lock:
            previous = list(analysis.batches)
        for saved in previous:
            if (
                saved.kind == "comparison"
                and saved.status
                in ("completed", "invalid", "failed", "outcome_unknown")
                and saved.candidate_ids == [item.candidate_id]
                and saved.result is not None
            ):
                rows = BatchComparisonSet.model_validate(saved.result)
                if [row.asin for row in rows.items] != saved.request.get("asins"):
                    raise DomainError(
                        409,
                        "HOT_SEARCH_RETRY_EVIDENCE_INVALID",
                        "已保存比较结果与商品请求不一致",
                    )
                if _comparison_failure_codes(saved) & {
                    "OUTCOME_UNKNOWN",
                    "MODEL_OUTCOME_UNKNOWN",
                }:
                    explicitly_replayed = any(
                        prior.id == saved.id
                        for prior in analysis.replayed_unknown_batches
                    )
                    if not explicitly_replayed:
                        raise DomainError(
                            409,
                            "HOT_SEARCH_RETRY_UNKNOWN_OUTCOME",
                            "存在结果未知的请求，请先核对分析记录",
                        )
                if saved.request.get("comparison_version") != COMPARISON_VERSION:
                    continue
                cached.update(
                    {
                        row.asin: row
                        for row in rows.items
                        if saved.request.get("profile_sha256")
                        == _fingerprint(
                            analysis.profile_snapshot.model_dump(mode="json")
                        )
                        and saved.request.get("snapshot_sha256", {}).get(row.asin)
                        == next(
                            (
                                _fingerprint(product.model_dump(mode="json"))
                                for product in requested
                                if product.asin == row.asin
                            ),
                            None,
                        )
                        and not row.exclusion_code.startswith("MODEL_")
                        and row.exclusion_code != "OUTCOME_UNKNOWN"
                    }
                )
        remaining = [product for product in requested if product.asin not in cached]
        reused, reuse_sources = self._reuse_completed_comparisons(
            analysis, item, remaining, previous
        )
        if reused:
            reuse_result = BatchComparisonSet(
                items=[
                    reused[product.asin]
                    for product in remaining
                    if product.asin in reused
                ]
            )
            with lock:
                analysis.batches.append(
                    AnalysisBatch(
                        kind="comparison",
                        candidate_ids=[item.candidate_id],
                        status="completed",
                        request={
                            "asins": [row.asin for row in reuse_result.items],
                            "execution": "reused_validated_comparison",
                            "comparison_version": COMPARISON_VERSION,
                            "snapshot_sha256": {
                                product.asin: _fingerprint(
                                    product.model_dump(mode="json")
                                )
                                for product in remaining
                                if product.asin in reused
                            },
                            "reuse_sources": reuse_sources,
                            "profile_sha256": _fingerprint(
                                analysis.profile_snapshot.model_dump(mode="json")
                            ),
                            "rule_version": analysis.rule_version,
                        },
                        result=reuse_result.model_dump(mode="json"),
                    )
                )
            cached.update(reused)
        products = [product for product in requested if product.asin not in cached]
        if not products:
            return BatchComparisonSet(
                items=[cached[product.asin] for product in requested]
            )
        requested_asins = {product.asin for product in products}
        replayed_unknown_ids = [
            str(saved.id)
            for saved in analysis.replayed_unknown_batches
            if item.candidate_id in saved.candidate_ids
            and requested_asins.intersection(saved.request.get("asins", []))
        ]
        batch = AnalysisBatch(
            kind="comparison",
            candidate_ids=[item.candidate_id],
            request={
                "asins": [product.asin for product in products],
                "comparison_version": COMPARISON_VERSION,
                "profile_sha256": _fingerprint(
                    analysis.profile_snapshot.model_dump(mode="json")
                ),
                "snapshot_sha256": {
                    product.asin: _fingerprint(product.model_dump(mode="json"))
                    for product in products
                },
                **(
                    {"replayed_unknown_batch_ids": replayed_unknown_ids}
                    if replayed_unknown_ids
                    else {}
                ),
            },
            status="started",
        )
        with lock:
            analysis.batches.append(batch)
            self._save_journal(analysis, batch)
        try:
            if model_semaphore is None:
                result = model.compare_product_batch(
                    analysis.profile_snapshot, products
                )
            else:
                with model_semaphore:
                    result = model.compare_product_batch(
                        analysis.profile_snapshot, products
                    )
            failed_rows = [
                row
                for row in result.items
                if row.exclusion_code.startswith("MODEL_")
                or row.exclusion_code == "OUTCOME_UNKNOWN"
            ]
            if failed_rows:
                # Per-row model retries can return an explicit failure instead of raising.
                # That is not a legitimate product exclusion or a completed paid result.
                with lock:
                    batch.result = result.model_dump(mode="json")
                code = next(
                    (
                        row.exclusion_code
                        for row in failed_rows
                        if row.exclusion_code
                        in ("OUTCOME_UNKNOWN", "MODEL_OUTCOME_UNKNOWN")
                    ),
                    failed_rows[0].exclusion_code,
                )
                raise DomainError(
                    502,
                    code,
                    "商品比较仍有模型处理失败项："
                    + ", ".join(row.asin for row in failed_rows),
                )
        except Exception as error:
            code = error.code if isinstance(error, DomainError) else "OUTCOME_UNKNOWN"
            retryable = code == "MODEL_OUTPUT_INVALID" or (
                isinstance(error, DomainError)
                and code == "MODEL_PROVIDER_ERROR"
                and error.status_code == 503
            )
            with lock:
                batch.error_code = code
                batch.error = str(error)[:500]
                batch.status = (
                    "invalid"
                    if retryable
                    else "outcome_unknown"
                    if code in ("OUTCOME_UNKNOWN", "MODEL_OUTCOME_UNKNOWN")
                    else "failed"
                )
                self._save_journal(analysis, batch)
            raise
        with lock:
            batch.status = "completed"
            batch.result = result.model_dump(mode="json")
            self._save_journal(analysis, batch, return_value=result)
        cached.update({row.asin: row for row in result.items})
        return BatchComparisonSet(items=[cached[product.asin] for product in requested])

    def _compare_with_fallback(
        self,
        analysis: HotSearchCoreAnalysis,
        item: HotSearchCoreItem,
        products: list[ProductSnapshot],
        model: QwenClient,
        lock: LockType,
        model_semaphore: BoundedSemaphore | None = None,
    ) -> BatchComparisonSet:
        try:
            return self._compare_once(
                analysis, item, products, model, lock, model_semaphore
            )
        except DomainError as error:
            if error.code != "MODEL_OUTPUT_INVALID" or len(products) <= 5:
                raise
        items = []
        for offset in range(0, len(products), 5):
            items.extend(
                self._compare_once(
                    analysis,
                    item,
                    products[offset : offset + 5],
                    model,
                    lock,
                    model_semaphore,
                ).items
            )
        return BatchComparisonSet(items=items)

    def _resume_evidence(
        self, analysis: HotSearchCoreAnalysis, existing: HotSearchCoreAnalysis
    ) -> None:
        """Copy known results into a new retry; keep the original journal immutable."""
        analysis.retry_of = existing.id
        intent_ids = {
            candidate_id
            for batch in existing.batches
            if batch.kind == "intent" and batch.status == "completed"
            for candidate_id in batch.candidate_ids
        }
        cutoff = datetime.now(UTC) - SHARED_SNAPSHOT_MAX_AGE
        analysis.snapshots = {
            asin: snapshot.model_copy(deep=True)
            for asin, snapshot in existing.snapshots.items()
            if snapshot.captured_at >= cutoff
        }
        previous = {item.candidate_id: item for item in existing.items}
        finished: set[UUID] = set()
        raw_ids: set[UUID] = set()
        same_version = existing.input_snapshot.get(
            "comparison_version"
        ) == analysis.input_snapshot.get("comparison_version")
        for index, item in enumerate(analysis.items):
            saved = previous.get(item.candidate_id)
            if saved is None or saved.keyword != item.keyword:
                continue
            if item.candidate_id in intent_ids and saved.intent_evidence is not None:
                item.intent_evidence = saved.intent_evidence.model_copy(deep=True)
                item.brand_category = saved.brand_category
            collection = existing.collections.get(str(item.candidate_id))
            unresolved = any(
                item.candidate_id in batch.candidate_ids
                and (
                    batch.status in ("failed", "outcome_unknown", "planned", "started")
                    or (
                        batch.status == "completed"
                        and bool(_comparison_failure_codes(batch))
                    )
                )
                for batch in existing.batches
            )
            raw_complete = (
                collection is not None
                and collection.term == item.keyword
                and _collection_failures_are_nonfatal(saved, collection)
                and _collection_snapshot_is_complete(collection)
                and existing.created_at >= cutoff
                and all(
                    product.captured_at >= cutoff for product in collection.products
                )
                and any(
                    batch.kind == "collection"
                    and batch.status == "completed"
                    and batch.candidate_ids == [item.candidate_id]
                    for batch in existing.batches
                )
            )
            if raw_complete:
                assert collection is not None
                analysis.collections[str(item.candidate_id)] = collection.model_copy(
                    deep=True
                )
                raw_ids.add(item.candidate_id)
                for product in collection.products:
                    analysis.snapshots.setdefault(
                        product.asin, product.model_copy(deep=True)
                    )
            versioned_scores = same_version and all(
                batch.request.get("comparison_version") == COMPARISON_VERSION
                for batch in existing.batches
                if batch.kind == "comparison"
                and item.candidate_id in batch.candidate_ids
            )
            if (
                raw_complete
                and (
                    versioned_scores
                    or (collection is not None and not collection.products)
                )
                and saved.status in ("completed", "no_samples")
                and item.intent_evidence is not None
                and existing.created_at >= cutoff
                and not unresolved
                and collection is not None
                and _collection_failures_are_nonfatal(saved, collection)
                and all(
                    product.captured_at >= cutoff for product in collection.products
                )
            ):
                analysis.items[index] = saved.model_copy(deep=True)
                finished.add(item.candidate_id)
                analysis.collections[str(item.candidate_id)] = collection.model_copy(
                    deep=True
                )

        def reusable_comparison(batch: AnalysisBatch) -> bool:
            asins = batch.request.get("asins")
            return (
                batch.kind == "comparison"
                and batch.request.get("comparison_version") == COMPARISON_VERSION
                and batch.result is not None
                and isinstance(asins, list)
                and bool(asins)
                and all(
                    isinstance(asin, str) and asin in analysis.snapshots
                    for asin in asins
                )
            )

        analysis.batches = [
            batch.model_copy(deep=True)
            for batch in existing.batches
            if batch.status == "completed"
            and not _comparison_failure_codes(batch)
            and (
                batch.kind == "intent"
                or reusable_comparison(batch)
                or (
                    batch.kind != "comparison"
                    and bool(batch.candidate_ids)
                    and set(batch.candidate_ids) <= (finished | raw_ids)
                )
            )
        ]
        # Raw outcomes are already stored in collections, snapshots, or item intent fields.
        # Keep comparison drafts because retry and cross-term reuse depend on them.
        for batch in analysis.batches:
            if batch.kind != "comparison":
                batch.result = None
        unknown_batches = [
            *existing.replayed_unknown_batches,
            *(
                batch
                for batch in existing.batches
                if batch.kind == "comparison"
                and (
                    batch.status == "outcome_unknown"
                    or bool(
                        _comparison_failure_codes(batch)
                        & {"OUTCOME_UNKNOWN", "MODEL_OUTCOME_UNKNOWN"}
                    )
                )
            ),
        ]
        analysis.replayed_unknown_batches = list(
            {
                str(batch.id): batch.model_copy(deep=True) for batch in unknown_batches
            }.values()
        )

    def start(
        self,
        project: UUID,
        run: UUID,
        report_id: UUID,
        own_brands: list[str] | None = None,
        *,
        retry_incomplete: bool = False,
        retry_unknown_model_outcomes: bool = False,
        force_reanalysis: bool = False,
    ) -> HotSearchCoreAnalysis:
        brands = sorted(
            {brand.strip().casefold() for brand in own_brands or [] if brand.strip()}
        )
        snapshot = self._input_snapshot(project, run, report_id, brands)
        fingerprint = _fingerprint(snapshot)
        items = self.core.hot_search_candidates(project, run, report_id)
        report = self.core.aba.get(project, run, report_id)
        analysis = HotSearchCoreAnalysis(
            project_id=project,
            run_id=run,
            report_id=report_id,
            fingerprint=fingerprint,
            profile_version=int(str(snapshot["profile_version"])),
            profile_snapshot=ProductProfile.model_validate(snapshot["profile"]),
            own_brands_snapshot=brands,
            marketplace=str(snapshot["marketplace"]),
            delivery_region=str(snapshot["delivery_region"]),
            input_snapshot=snapshot,
            source_partial=any(query.truncated for query in report.reports),
            items=items,
        )
        # Claim once in a short write transaction. No model configuration or network is loaded here.
        with self.repository.connect() as conn:
            current = conn.execute(
                "SELECT document FROM hot_search_core_analyses WHERE id=("
                "SELECT analysis_id FROM hot_search_core_current WHERE run_id=?)",
                (str(run),),
            ).fetchone()
            current_analysis = (
                HotSearchCoreAnalysis.model_validate_json(current[0])
                if current
                else None
            )
            comparison_refresh = bool(
                current_analysis
                and _compatible_comparison_refresh(
                    current_analysis.input_snapshot, snapshot
                )
            )
            if (
                comparison_refresh
                and current_analysis is not None
                and current_analysis.status in ("queued", "running")
            ):
                return current_analysis
            if force_reanalysis:
                if current_analysis is not None and current_analysis.status in (
                    "queued",
                    "running",
                ):
                    return current_analysis
                forced_fingerprint = hashlib.sha256(
                    f"{fingerprint}:forced:{uuid4()}".encode()
                ).hexdigest()
                conn.execute(
                    "INSERT INTO hot_search_core_analyses (id, run_id, fingerprint, document) "
                    "VALUES(?,?,?,?)",
                    (
                        str(analysis.id),
                        str(run),
                        forced_fingerprint,
                        analysis.model_dump_json(),
                    ),
                )
                conn.execute(
                    "INSERT INTO hot_search_core_current(run_id, analysis_id) VALUES(?,?) "
                    "ON CONFLICT(run_id) DO UPDATE SET analysis_id=excluded.analysis_id",
                    (str(run), str(analysis.id)),
                )
                return analysis
            conn.execute(
                "INSERT INTO hot_search_core_analyses (id, run_id, fingerprint, document) "
                "VALUES(?,?,?,?) ON CONFLICT (run_id, fingerprint) DO NOTHING",
                (str(analysis.id), str(run), fingerprint, analysis.model_dump_json()),
            )
            saved = conn.execute(
                "SELECT document FROM hot_search_core_analyses WHERE run_id=? AND fingerprint=?",
                (str(run), fingerprint),
            ).fetchone()
            assert saved is not None
            completed = _completed_document_for_fingerprint(
                [
                    row[0]
                    for row in conn.execute(
                        "SELECT document FROM hot_search_core_analyses WHERE run_id=? ORDER BY seq DESC",
                        (str(run),),
                    ).fetchall()
                ],
                fingerprint,
            )
            if completed is not None:
                saved = (completed,)
            existing = HotSearchCoreAnalysis.model_validate_json(saved[0])
            claimed = existing
            if retry_incomplete or comparison_refresh:
                current = conn.execute(
                    "SELECT document FROM hot_search_core_analyses WHERE id=("
                    "SELECT analysis_id FROM hot_search_core_current WHERE run_id=?)",
                    (str(run),),
                ).fetchone()
                if current:
                    current_analysis = HotSearchCoreAnalysis.model_validate_json(
                        current[0]
                    )
                    if (
                        current_analysis.fingerprint == fingerprint
                        or comparison_refresh
                    ):
                        existing = current_analysis
                        saved = current
            # A browser-stage failure is retryable. The old implementation returned the same
            # terminal failed document forever because the fingerprint was idempotent, so the
            # workflow's "continue" button could never actually retry collection. Preserve the
            # failed evidence and allocate a new fingerprint/id for an explicit retry, but never
            # replay an analysis with an unknown model outcome.
            uncertain = any(
                batch.status in ("outcome_unknown", "started")
                or bool(
                    _comparison_failure_codes(batch)
                    & {"OUTCOME_UNKNOWN", "MODEL_OUTCOME_UNKNOWN"}
                )
                for batch in existing.batches
            )
            if (retry_incomplete or comparison_refresh) and existing.status in (
                "completed",
                "incomplete",
                "failed",
            ):
                replayable_unknown = [
                    *existing.replayed_unknown_batches,
                    *(
                        batch
                        for batch in existing.batches
                        if batch.kind == "comparison"
                        and (
                            batch.status == "outcome_unknown"
                            or bool(
                                _comparison_failure_codes(batch)
                                & {"OUTCOME_UNKNOWN", "MODEL_OUTCOME_UNKNOWN"}
                            )
                        )
                    ),
                ]
                uncertain_model = any(
                    batch.status == "started"
                    or (
                        batch.kind != "comparison"
                        and (
                            batch.status == "outcome_unknown"
                            or bool(
                                _comparison_failure_codes(batch)
                                & {"OUTCOME_UNKNOWN", "MODEL_OUTCOME_UNKNOWN"}
                            )
                        )
                    )
                    for batch in existing.batches
                )
                if uncertain_model or (
                    replayable_unknown and not retry_unknown_model_outcomes
                ):
                    raise DomainError(
                        409,
                        "HOT_SEARCH_RETRY_UNKNOWN_OUTCOME",
                        "存在结果未知的请求，请先核对分析记录",
                    )
                if retry_unknown_model_outcomes and any(
                    not batch.candidate_ids
                    or not isinstance(batch.request.get("asins"), list)
                    or not batch.request.get("asins")
                    for batch in replayable_unknown
                ):
                    raise DomainError(
                        409,
                        "HOT_SEARCH_RETRY_EVIDENCE_INVALID",
                        "结果未知的比较批次缺少商品或候选词信息",
                    )
                if existing.source_partial:
                    raise DomainError(
                        409,
                        "HOT_SEARCH_SOURCE_INCOMPLETE",
                        "热门搜索词来源不完整，请先重新采集周报",
                    )
            retryable = (
                comparison_refresh
                or (existing.status == "failed" and not uncertain)
                or (retry_incomplete and existing.status in ("incomplete", "failed"))
            )
            if retryable:
                # Keep the input fingerprint in the document; only the unique DB key changes.
                # Otherwise _view() marks this valid retry stale immediately after creation.
                retry_fingerprint = hashlib.sha256(
                    f"{fingerprint}:browser-retry:{uuid4()}".encode()
                ).hexdigest()
                if comparison_refresh:
                    analysis.id = claimed.id
                else:
                    analysis.id = uuid4()
                self._resume_evidence(analysis, existing)
                conn.execute(
                    "INSERT INTO hot_search_core_analyses (id, run_id, fingerprint, document) "
                    "VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET document=excluded.document",
                    (
                        str(analysis.id),
                        str(run),
                        retry_fingerprint,
                        analysis.model_dump_json(),
                    ),
                )
                saved = conn.execute(
                    "SELECT document FROM hot_search_core_analyses WHERE id=?",
                    (str(analysis.id),),
                ).fetchone()
                assert saved is not None
            analysis = HotSearchCoreAnalysis.model_validate_json(saved[0])
            # Activation and idempotent creation share the same write lock. Evidence is immutable.
            conn.execute(
                "INSERT INTO hot_search_core_current(run_id, analysis_id) VALUES(?,?) "
                "ON CONFLICT(run_id) DO UPDATE SET analysis_id=excluded.analysis_id",
                (str(run), str(analysis.id)),
            )
        return analysis

    def _call(
        self,
        analysis: HotSearchCoreAnalysis,
        kind: BatchKind,
        ids: list[UUID],
        request: dict[str, object],
        operation: Callable[[], T],
        *,
        model: bool = False,
        state_lock: LockType | None = None,
        result_sink: Callable[[T], None] | None = None,
        store_result: bool = True,
    ) -> T:
        for attempt in range(1, 4 if model else 2):
            started_clock = monotonic()
            with state_lock if state_lock is not None else nullcontext():
                batch = AnalysisBatch(
                    kind=kind,
                    candidate_ids=ids,
                    request=request,
                    attempt=attempt,
                    status="started",
                    started_at=datetime.now(UTC),
                )
                analysis.batches.append(batch)
                self._save_journal(analysis, batch)
            try:
                result = operation()
            except Exception as error:
                batch.finished_at = datetime.now(UTC)
                batch.duration_ms = max(0, int((monotonic() - started_clock) * 1000))
                code = (
                    error.code if isinstance(error, DomainError) else "OUTCOME_UNKNOWN"
                )
                with state_lock if state_lock is not None else nullcontext():
                    batch.error_code = code
                    batch.error = str(error)[:500]
                    batch.status = (
                        "invalid"
                        if code == "MODEL_OUTPUT_INVALID"
                        else "outcome_unknown"
                        if code in ("OUTCOME_UNKNOWN", "MODEL_OUTCOME_UNKNOWN")
                        else "failed"
                    )
                    self._save_journal(analysis, batch)
                if model and code == "MODEL_OUTPUT_INVALID" and attempt < 3:
                    continue
                raise
            with state_lock if state_lock is not None else nullcontext():
                if result_sink is not None:
                    result_sink(result)
                batch.status = "completed"
                batch.finished_at = datetime.now(UTC)
                batch.duration_ms = max(0, int((monotonic() - started_clock) * 1000))
                if store_result and isinstance(result, BaseModel):
                    batch.result = result.model_dump(mode="json")
                self._save_journal(analysis, batch, return_value=result)
            return result
        raise AssertionError("unreachable")

    def _finalize(self, analysis: HotSearchCoreAnalysis) -> None:
        analysis.items = sort_core_items(analysis.items, analysis.rule_version)
        analysis.recommended_candidate_ids = [
            item.candidate_id
            for item in analysis.items
            if is_core_eligible(item, analysis.rule_version)
        ]
        failed = sum(item.status == "failed" for item in analysis.items)
        items_by_id = {str(item.candidate_id): item for item in analysis.items}
        collection_failed = any(
            candidate_id not in items_by_id
            or not _collection_failures_are_nonfatal(items_by_id[candidate_id], result)
            for candidate_id, result in analysis.collections.items()
        )
        unresolved = any(
            batch.status in ("failed", "outcome_unknown", "planned", "started")
            or (batch.status == "completed" and bool(_comparison_failure_codes(batch)))
            for batch in analysis.batches
        )
        # Invalid model output followed by a successful bounded retry is resolved.
        # Identity-mismatched catalog pages are excluded evidence, not transport failures.
        # Other product-detail failures still keep the analysis incomplete.
        analysis.complete = not (
            failed or analysis.source_partial or collection_failed or unresolved
        )
        analysis.status = (
            "failed"
            if analysis.items and failed == len(analysis.items)
            else "completed"
            if analysis.complete
            else "incomplete"
        )

    def _analyze_term(
        self,
        analysis: HotSearchCoreAnalysis,
        item: HotSearchCoreItem,
        session: ProductSearchSession,
        model: QwenClient,
        *,
        state_lock: LockType | None = None,
        model_semaphore: BoundedSemaphore | None = None,
    ) -> None:
        journal_lock = state_lock or Lock()
        with journal_lock:
            item.status = "running"
        collected = analysis.collections.get(str(item.candidate_id))
        cutoff = datetime.now(UTC) - SHARED_SNAPSHOT_MAX_AGE
        reusable = (
            analysis.retry_of is not None
            and analysis.created_at >= cutoff
            and collected is not None
            and collected.term == item.keyword
            and not collected.failures
            and len(collected.products) == len(collected.unique_asins)
            and len(set(collected.unique_asins)) == len(collected.unique_asins)
            and {product.asin for product in collected.products}
            == set(collected.unique_asins)
            and all(product.captured_at >= cutoff for product in collected.products)
            and any(
                batch.kind == "collection"
                and batch.status == "completed"
                and batch.candidate_ids == [item.candidate_id]
                for batch in analysis.batches
            )
        )
        if not reusable:
            collected = self._call(
                analysis,
                "collection",
                [item.candidate_id],
                {"keyword": item.keyword, "limit": 30},
                lambda: collect_natural_products(
                    session, item.keyword, 30, f"term-{item.candidate_id}"
                ),
                state_lock=journal_lock,
                result_sink=lambda value: analysis.collections.__setitem__(
                    str(item.candidate_id), value
                ),
                store_result=False,
            )
        else:
            assert collected is not None
            with journal_lock:
                analysis.batches.append(
                    AnalysisBatch(
                        kind="collection",
                        candidate_ids=[item.candidate_id],
                        status="completed",
                        request={
                            "keyword": item.keyword,
                            "limit": 30,
                            "execution": "reused_raw_collection",
                            "source_analysis_id": str(analysis.retry_of),
                        },
                    )
                )
        assert collected is not None
        positions = {
            asin: index for index, asin in enumerate(collected.unique_asins, 1)
        }
        with journal_lock:
            item.comparisons = [
                ProductComparison(
                    asin=failure.asin,
                    status="excluded",
                    exclusion_reason=failure.reason,
                    natural_position=positions.get(failure.asin),
                )
                for failure in collected.failures
            ]
        product_batches = [
            collected.products[offset : offset + 10]
            for offset in range(0, len(collected.products), 10)
        ]

        def save_batch(
            products: list[ProductSnapshot], batch: BatchComparisonSet
        ) -> None:
            for snapshot, draft in zip(products, batch.items, strict=True):
                if not draft.valid:
                    comparison = ProductComparison(
                        asin=snapshot.asin,
                        status="excluded",
                        exclusion_reason=draft.exclusion_reason,
                    )
                else:
                    candidate = ProductProfile(
                        asin=snapshot.asin,
                        purpose=draft.purpose,
                        style_structure=draft.style_structure,
                        specifications=draft.specifications,
                        package_quantity=draft.package_quantity,
                        unit_basis=draft.unit_basis,
                        price=snapshot.price if snapshot.price_reliable else None,
                        currency=snapshot.currency if snapshot.price_reliable else None,
                    )
                    assert draft.scores is not None
                    comparison = score_product(
                        analysis.profile_snapshot,
                        candidate,
                        draft.scores,
                        high_related_threshold=(
                            Decimal("40")
                            if analysis.rule_version == "hot-search-core-v2"
                            else Decimal("60")
                        ),
                        inclusive=analysis.rule_version == "hot-search-core-v2",
                    )
                    comparison.candidate_profile = candidate
                comparison.natural_position = positions.get(snapshot.asin)
                item.comparisons.append(comparison)
            item.comparisons.sort(
                key=lambda comparison: comparison.natural_position or 31
            )

        rate_limited: list[list[ProductSnapshot]] = []
        first_error: Exception | None = None
        concurrency = max(1, min(_model_concurrency(), len(product_batches)))
        with ThreadPoolExecutor(
            max_workers=concurrency,
            thread_name_prefix="core-relevance",
        ) as executor:
            futures: dict[Future[BatchComparisonSet], list[ProductSnapshot]] = {
                executor.submit(
                    self._compare_with_fallback,
                    analysis,
                    item,
                    products,
                    model,
                    journal_lock,
                    model_semaphore,
                ): products
                for products in product_batches
            }
            for future in as_completed(futures):
                products = futures[future]
                try:
                    batch = future.result()
                except DomainError as error:
                    if (
                        error.code == "MODEL_PROVIDER_ERROR"
                        and error.status_code == 503
                    ):
                        rate_limited.append(products)
                    elif first_error is None:
                        first_error = error
                except Exception as error:
                    if first_error is None:
                        first_error = error
                else:
                    with journal_lock:
                        save_batch(products, batch)
        if first_error is not None:
            raise first_error
        # Upstream 429 batches are retried only after all concurrent calls have settled.
        for products in sorted(
            rate_limited,
            key=lambda rows: positions.get(rows[0].asin, 31),
        ):
            batch = self._compare_with_fallback(
                analysis, item, products, model, journal_lock, model_semaphore
            )
            with journal_lock:
                save_batch(products, batch)
        with journal_lock:
            item.comparisons.sort(
                key=lambda comparison: comparison.natural_position or 31
            )
            item.summary = summarize_term(item.comparisons)
            item.status = "completed" if item.summary.valid_samples else "no_samples"
            self._save(analysis, checkpoint_candidate_id=item.candidate_id)

    def execute(
        self, project: UUID, run: UUID, analysis_id: UUID
    ) -> HotSearchCoreAnalysis:
        # Only a queued analysis can claim execution; running and terminal documents never resend.
        with self.repository.connect() as conn:
            analysis = self._load(project, run, analysis_id)
            if analysis.status != "queued":
                return self._view(analysis)
            analysis = self._view(analysis)
            if analysis.stale:
                return analysis
            analysis.status = "running"
            conn.execute(
                "UPDATE hot_search_core_analyses SET document=? WHERE id=?",
                (analysis.model_dump_json(), str(analysis.id)),
            )
        try:
            model = self.client or QwenClient(
                QwenConfig.load(self.repository.storage_root)
            )
            model.config.validate()
            state_lock = Lock()
            detail_lock = Lock()
            model_semaphore = BoundedSemaphore(_model_concurrency())
            shared = self._shared_product_snapshots(run)
            for asin, snapshot in shared.items():
                analysis.snapshots.setdefault(asin, snapshot)
            if shared:
                self._save(analysis)
            for offset in range(0, len(analysis.items), 20):
                terms = [
                    item
                    for item in analysis.items[offset : offset + 20]
                    if item.intent_evidence is None
                ]
                if not terms:
                    continue
                try:

                    def commit_intents(draft: object) -> None:
                        judgments = {
                            judgment.candidate_id: judgment for judgment in draft.items
                        }
                        for item in terms:
                            item.brand_category = judgments[
                                item.candidate_id
                            ].brand_category
                            item.intent_evidence = judgments[
                                item.candidate_id
                            ].intent_evidence

                    self._call(
                        analysis,
                        "intent",
                        [item.candidate_id for item in terms],
                        {"keywords": [item.keyword for item in terms]},
                        partial(
                            model.classify_hot_search_intents,
                            analysis.profile_snapshot,
                            terms,
                            analysis.own_brands_snapshot,
                        ),
                        model=True,
                        state_lock=state_lock,
                        result_sink=commit_intents,
                        store_result=False,
                    )
                except Exception as error:
                    with state_lock:
                        for item in terms:
                            item.status = "failed"
                            item.exclusion_reasons.append(str(error)[:500])
                self._save(analysis, checkpoint_kind="intent")

            service = self

            class JournaledSession:
                def __init__(
                    self, raw: ProductSearchSession, current: HotSearchCoreItem
                ) -> None:
                    self.raw = raw
                    self.current = current

                def search(
                    self, keyword: str, page_number: int, evidence_name: str
                ) -> SearchPage:
                    def read() -> SearchPage:
                        page = self.raw.search(keyword, page_number, evidence_name)
                        if analysis.marketplace == "US":
                            require_us_delivery(page.region, analysis.delivery_region)
                        return page

                    return service._call(
                        analysis,
                        "search",
                        [self.current.candidate_id],
                        {"keyword": keyword, "page_number": page_number},
                        read,
                        state_lock=state_lock,
                        store_result=False,
                    )

                def product(self, asin: str) -> ProductSnapshot:
                    with detail_lock:
                        with state_lock:
                            if asin in analysis.snapshot_failures:
                                raise DomainError(
                                    409,
                                    "PRODUCT_DETAIL_ALREADY_ATTEMPTED",
                                    analysis.snapshot_failures[asin],
                                )
                            snapshot = analysis.snapshots.get(asin)
                        if snapshot is None:
                            try:
                                service._call(
                                    analysis,
                                    "product",
                                    [self.current.candidate_id],
                                    {"asin": asin},
                                    lambda: self.raw.product(asin),
                                    state_lock=state_lock,
                                    result_sink=lambda value: (
                                        analysis.snapshots.__setitem__(asin, value)
                                    ),
                                    store_result=False,
                                )
                            except Exception as error:
                                with state_lock:
                                    if not (
                                        isinstance(error, DomainError)
                                        and error.code in PAUSE_CODES
                                    ):
                                        analysis.snapshot_failures[asin] = str(error)[
                                            :500
                                        ]
                                raise
                        with state_lock:
                            return analysis.snapshots[asin]

                def products(
                    self, asins: list[str]
                ) -> list[tuple[str, ProductSnapshot | None, Exception | None]]:
                    with detail_lock:
                        results: dict[
                            str, tuple[str, ProductSnapshot | None, Exception | None]
                        ] = {}
                        missing: list[str] = []
                        with state_lock:
                            for asin in asins:
                                if asin in analysis.snapshots:
                                    results[asin] = (
                                        asin,
                                        analysis.snapshots[asin],
                                        None,
                                    )
                                elif asin in analysis.snapshot_failures:
                                    results[asin] = (
                                        asin,
                                        None,
                                        DomainError(
                                            409,
                                            "PRODUCT_DETAIL_ALREADY_ATTEMPTED",
                                            analysis.snapshot_failures[asin],
                                        ),
                                    )
                                else:
                                    missing.append(asin)
                        if missing:
                            fetch_many = getattr(self.raw, "products", None)

                            def fetch():
                                if callable(fetch_many):
                                    return fetch_many(missing)
                                fetched = []
                                for asin in missing:
                                    try:
                                        fetched.append(
                                            (asin, self.raw.product(asin), None)
                                        )
                                    except Exception as error:
                                        fetched.append((asin, None, error))
                                return fetched

                            def commit_products(fetched):
                                for asin, snapshot, error in fetched:
                                    if snapshot is not None and error is None:
                                        analysis.snapshots[asin] = snapshot
                                    elif not (
                                        isinstance(error, DomainError)
                                        and error.code
                                        in PAUSE_CODES | PRODUCT_RETRY_CODES
                                    ):
                                        analysis.snapshot_failures[asin] = (
                                            str(error) or type(error).__name__
                                        )[:500]
                                    results[asin] = (asin, snapshot, error)

                            service._call(
                                analysis,
                                "product",
                                [self.current.candidate_id],
                                {"asins": missing},
                                fetch,
                                state_lock=state_lock,
                                result_sink=commit_products,
                                store_result=False,
                            )
                        return [results[asin] for asin in asins]

            if any(item.status == "waiting" for item in analysis.items):
                context: AbstractContextManager[SearchSession] | None = None

                def open_session() -> SearchSession:
                    nonlocal context
                    context = self.search.open(
                        analysis.marketplace,
                        self.repository.storage_root / "evidence" / str(analysis.id),
                    )
                    return context.__enter__()

                raw = self._call(
                    analysis,
                    "open",
                    [],
                    {"marketplace": analysis.marketplace},
                    open_session,
                )
                try:

                    def analyze_one(current: HotSearchCoreItem) -> None:
                        if current.status != "waiting":
                            return
                        session = JournaledSession(
                            cast(ProductSearchSession, raw), current
                        )
                        try:
                            self._analyze_term(
                                analysis,
                                current,
                                session,
                                model,
                                state_lock=state_lock,
                                model_semaphore=model_semaphore,
                            )
                        except Exception as error:
                            with state_lock:
                                current.status = "failed"
                                current.exclusion_reasons.append(str(error)[:500])
                                self._save(
                                    analysis,
                                    checkpoint_candidate_id=current.candidate_id,
                                )

                    pending_terms = [
                        item for item in analysis.items if item.status == "waiting"
                    ]
                    term_workers = _term_concurrency(self.search)
                    if term_workers == 1:
                        for current in pending_terms:
                            analyze_one(current)
                    else:
                        with ThreadPoolExecutor(
                            max_workers=term_workers,
                            thread_name_prefix="core-term",
                        ) as executor:
                            futures = [
                                submit_with_browser_operation_context(
                                    executor, analyze_one, current
                                )
                                for current in pending_terms
                            ]
                            for future in as_completed(futures):
                                future.result()
                finally:
                    assert context is not None
                    context.__exit__(None, None, None)
        except Exception as error:
            for item in analysis.items:
                if item.status in ("waiting", "running"):
                    item.status = "failed"
                    item.exclusion_reasons.append(str(error)[:500])
        self._finalize(analysis)
        self._save(analysis, clear_journal=True)
        return self._view(analysis)

    def recover(self) -> None:
        with self.repository.connect() as conn:
            rows = conn.execute(
                "SELECT id,document FROM hot_search_core_analyses"
            ).fetchall()
        for analysis_id, document in rows:
            analysis = HotSearchCoreAnalysis.model_validate_json(document)
            if analysis.status not in ("queued", "running"):
                continue
            self._merge_journal(analysis)
            with self.repository.connect() as conn:
                journal = conn.execute(
                    "SELECT 1 FROM hot_search_core_analysis_journal "
                    "WHERE analysis_id=? LIMIT 1",
                    (str(analysis_id),),
                ).fetchone()
            if (
                analysis.status == "queued"
                and journal is None
                and not any(
                    batch.status in ("planned", "started") for batch in analysis.batches
                )
            ):
                continue
            for batch in analysis.batches:
                if batch.status in ("planned", "started"):
                    batch.status = "outcome_unknown"
                    batch.error_code = "PROCESS_RESTARTED"
                    batch.error = "服务重启时结果未知，未自动重发"
            for item in analysis.items:
                if item.status in ("waiting", "running"):
                    item.status = "failed"
                    item.exclusion_reasons.append("服务重启时结果未知，未自动重发")
            self._finalize(analysis)
            self._save(analysis, clear_journal=True)
