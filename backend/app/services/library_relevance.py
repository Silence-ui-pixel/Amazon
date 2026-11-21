"""Persisted, bounded Excel verification jobs; no writes to existing business data."""

import hashlib
import json
import logging
import os
from collections.abc import Callable
from concurrent.futures import Executor, ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Event, RLock, Thread
from time import monotonic
from typing import Any, cast
from uuid import UUID, uuid4

from app.adapters.browser_tasks import BrowserTaskExecutor
from app.adapters.library_template import export_xlsx
from app.adapters.remote_browser import (
    browser_device_context,
    browser_operation_context,
    current_browser_device_id,
)
from app.db.repository import Repository
from app.domain.asin_product import ProductSnapshot
from app.domain.errors import DomainError
from app.domain.library import LibraryReport
from app.domain.library_relevance import (
    RULE_VERSION,
    SAMPLE_LIMIT,
    RelevanceExport,
    RelevanceResult,
)
from app.domain.product_terms import ProductProfile, ProductProfileRecord
from app.domain.text import fold_text
from app.domain.verification import CollectionResult, ProductCollectionFailure, SearchPage
from app.services.amazon_search import SearchProvider
from app.services.library import LibraryService
from app.services.library_relevance_model import classify_unscorable
from app.services.product_comparison_semantics import COMPARISON_VERSION
from app.services.product_profiles import ProductProfileService
from app.services.product_term_collection import ProductSearchSession, collect_natural_products
from app.services.product_term_scoring import score_product
from app.services.qwen import QwenClient, QwenConfig
from app.services.search_policy import PAUSE_CODES

log = logging.getLogger(__name__)
# Agent reachability does not imply that its dedicated shop browser is open.
# Keep this Excel-only so the existing workflow pause policy stays unchanged.
EXCEL_PAUSE_CODES = PAUSE_CODES | {"ZINIAO_CDP_UNAVAILABLE"}


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _write(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _read(path: Path) -> Any:  # noqa: ANN401 - JSON evidence has provider-defined fields.
    return json.loads(path.read_text(encoding="utf-8"))


class LibraryRelevanceService:
    def __init__(
        self, repository: Repository, library: LibraryService, profiles: ProductProfileService,
        search: SearchProvider, client: QwenClient | None, *, auto_execute: bool = True,
        executor: Executor | None = None,
        collector_ready: Callable[[], bool] | None = None,
    ) -> None:
        self.repository, self.library, self.profiles = repository, library, profiles
        self.search, self.client = search, client
        self.root = repository.storage_root / "library_relevance"
        self.lock = RLock()
        # Production shares the workflow device queue and its existing worker limit.
        self._owns_executor = executor is None
        self.executor = executor if executor is not None else BrowserTaskExecutor(
            thread_name_prefix="library-relevance", max_workers=1,
        )
        self.auto_execute = auto_execute
        self.collector_ready = collector_ready
        self._scheduled: set[UUID] = set()
        self._stop = Event()
        self._scheduler: Thread | None = None
        # Only Excel comparison calls overlap. Browser reads retain the existing queue.
        try:
            workers = int(os.getenv("KEYWORD_CORE_MODEL_CONCURRENCY", "12") or "12")
        except ValueError:
            workers = 12
        self._model_pool = ThreadPoolExecutor(max_workers=max(1, min(3, workers)),
                                               thread_name_prefix="excel-model")

    def use_executor(self, executor: Executor) -> None:
        if self._owns_executor:
            self.executor.shutdown(wait=False)
        self.executor, self._owns_executor = executor, False

    def _submit(self, job: RelevanceExport) -> None:
        if not self.auto_execute or self._stop.is_set():
            return
        with self.lock:
            if job.id in self._scheduled:
                return
            self._scheduled.add(job.id)
        def work() -> None:
            try:
                self.execute(job)
            finally:
                with self.lock:
                    self._scheduled.discard(job.id)
        try:
            with browser_device_context(job.device_id):
                self.executor.submit(work)
        except Exception:
            with self.lock:
                self._scheduled.discard(job.id)
            raise

    def _tick(self) -> None:
        for path in self.root.glob("*/*/*/manifest.json"):
            try:
                job = RelevanceExport.model_validate(_read(path))
                if job.status == "queued" or (
                    job.status == "failed" and job.next_retry_at is not None
                    and job.next_retry_at <= datetime.now(UTC)
                ):
                    self._submit(job)
            except Exception:
                log.exception("Excel relevance recovery failed for %s", path)

    def ensure(self, project: UUID, run: UUID, library: UUID, ranked_only: bool = True) -> RelevanceExport:
        """Idempotent automatic entry, also used by already-created libraries."""
        job = self.start(project, run, library, ranked_only)
        if job.status == "queued":
            self._submit(job)
        return job

    def _directory(self, project: UUID, run: UUID, key: UUID) -> Path:
        return self.root / str(project) / str(run) / str(key)

    def _save(self, job: RelevanceExport) -> None:
        with self.lock:
            job.updated_at = datetime.now(UTC)
            _write(self._directory(job.project_id, job.run_id, job.id) / "manifest.json",
                   job.model_dump(mode="json"))

    def _profile(self, report: LibraryReport) -> ProductProfileRecord:
        # Historical libraries use the profile confirmed when that library was created.
        profiles = [p for p in self.profiles.history(report.project_id, report.run_id)
                    if p.status == "confirmed" and p.created_at <= report.created_at]
        if not profiles:
            raise DomainError(409, "PRODUCT_PROFILE_REQUIRED", "未找到该词库生成时的已确认商品四维资料，请先确认资料并生成对应词库")
        return max(profiles, key=lambda p: p.version)

    def get(self, project: UUID, run: UUID, library: UUID, key: UUID) -> RelevanceExport:
        self.repository.get_run(project, run)
        with self.lock:
            path = self._directory(project, run, key) / "manifest.json"
            if not path.is_file():
                raise DomainError(404, "NOT_FOUND", "相关性核验任务不存在")
            job = RelevanceExport.model_validate(_read(path))
            if job.library_id != library or job.project_id != project or job.run_id != run:
                raise DomainError(404, "NOT_FOUND", "相关性核验任务不存在")
            return job

    def _identity(self, report: LibraryReport, ranked_only: bool, *, legacy: bool = False) -> str:
        profile = self._profile(report)
        config = self._model().config
        pipeline = self.repository.get_run(report.project_id, report.run_id)
        keywords = [e.keyword for e in report.entries if not ranked_only or (e.evidence.metrics.rank or 0) > 0]
        # Draft version, attachment archives and presentation fields do not change a product judgment.
        library_basis = report.model_dump(mode="json", exclude={"stale"}) if legacy else {
            "project_id": str(report.project_id), "run_id": str(report.run_id), "id": str(report.id),
        }
        return _digest({
            "library": library_basis,
            "profile": profile.model_dump(mode="json"), "keywords": keywords, "ranked_only": ranked_only,
            "rule": RULE_VERSION, "comparison": COMPARISON_VERSION,
            "model": config.model, "endpoint": config.base_url, "protocol": config.protocol,
            "marketplace": pipeline.input.marketplace,
            "region": os.getenv("KEYWORD_US_DELIVERY_ZIP", "10001").strip(), "sample_limit": SAMPLE_LIMIT,
        })

    def _matches(self, job: RelevanceExport, report: LibraryReport, ranked_only: bool, identity: str) -> bool:
        if (job.project_id, job.run_id, job.library_id, job.ranked_only) != (
            report.project_id, report.run_id, report.id, ranked_only
        ):
            return False
        if job.fingerprint == identity:
            return True
        # Recognize existing production jobs without rewriting their progress or frozen evidence.
        path = self._directory(job.project_id, job.run_id, job.id) / "evidence" / "library.json"
        if not path.is_file():
            return False
        frozen = LibraryReport.model_validate(_read(path))
        return (self._identity(frozen, ranked_only, legacy=True) == job.fingerprint
                and self._identity(frozen, ranked_only) == identity)

    def latest(self, project: UUID, run: UUID, library: UUID, ranked_only: bool) -> RelevanceExport | None:
        report = self.library.get(project, run, library)
        with self.lock:
            jobs = [RelevanceExport.model_validate(_read(p)) for p in
                    (self.root / str(project) / str(run)).glob("*/manifest.json")]
            candidates = [j for j in jobs if j.library_id == library and j.ranked_only == ranked_only]
            cutoff = datetime.now(UTC) - timedelta(hours=24)
            identity = self._identity(report, ranked_only) if candidates else ""
            candidates = [j for j in candidates if (j.created_at >= cutoff or j.status in ("queued", "running"))
                          and self._matches(j, report, ranked_only, identity)]
            if not candidates:
                return None
            # An old finished job must not be hidden by an already queued duplicate.
            job = max(candidates, key=lambda j: (j.status == "completed", j.completed, j.created_at))
            return job.model_copy(update={"library_version": report.version})

    def _model(self) -> QwenClient:
        client = self.client or QwenClient(
            QwenConfig.load(self.repository.storage_root),
            diagnostic_dir=self.root / "model_diagnostics",
        )
        client.config.validate()
        return client

    def start(self, project: UUID, run: UUID, library: UUID, ranked_only: bool) -> RelevanceExport:
        report = self.library.get(project, run, library)
        profile = self._profile(report)
        confirmation = self.profiles.confirmations.get(project, run)
        frozen = json.loads(report.snapshots.get("product_confirmation", "{}"))
        if not frozen or frozen.get("data", {}).get("product") != confirmation.data.product.model_dump(mode="json"):
            raise DomainError(409, "LIBRARY_PRODUCT_CHANGED", "词库对应商品资料已变化，请生成对应版本词库后核验")
        client = self._model()
        pipeline = self.repository.get_run(project, run)
        region = os.getenv("KEYWORD_US_DELIVERY_ZIP", "10001").strip()
        keywords = [e.keyword for e in report.entries if not ranked_only or (e.evidence.metrics.rank or 0) > 0]
        fingerprint = self._identity(report, ranked_only)
        with self.lock:
            existing = self.latest(project, run, library, ranked_only)
            if existing is not None:
                return existing
            job = RelevanceExport(
                id=uuid4(), project_id=project, run_id=run, library_id=library,
                fingerprint=fingerprint, library_version=report.version, ranked_only=ranked_only,
                model=client.config.model, model_endpoint=client.config.base_url,
                model_protocol=client.config.protocol, device_id=current_browser_device_id(),
                marketplace=pipeline.input.marketplace, delivery_region=region,
                profile=profile.profile, profile_version=profile.version, total=len(keywords),
            )
            directory = self._directory(project, run, job.id)
            _write(directory / "results.json", [RelevanceResult(keyword=k).model_dump(mode="json") for k in keywords])
            _write(directory / "evidence" / "library.json", report.model_dump(mode="json"))
            self._save(job)
            self._submit(job)
            return job

    def retry(self, project: UUID, run: UUID, library: UUID, key: UUID) -> RelevanceExport:
        with self.lock:
            job = self.get(project, run, library, key)
            if job.status != "failed":
                return job
            if job.created_at < datetime.now(UTC) - timedelta(hours=24):
                raise DomainError(409, "RELEVANCE_EVIDENCE_EXPIRED", "核验证据已过期，请重新生成相关性任务")
            # Explicit retry may use a newly reconnected collector, still scoped to this task.
            job.device_id = current_browser_device_id() or job.device_id
            job.status, job.error = "queued", ""
            job.next_retry_at = None
            path = self._directory(project, run, key) / "results.json"
            results = [RelevanceResult.model_validate(r) for r in _read(path)]
            for result in results:
                if result.status != "completed":
                    result.attempts = 0
            _write(path, [r.model_dump(mode="json") for r in results])
            self._save(job)
            self._submit(job)
            return job

    def recover(self) -> None:
        with self.lock:
            for path in self.root.glob("*/*/*/manifest.json"):
                job = RelevanceExport.model_validate(_read(path))
                results_path = path.parent / "results.json"
                results = [RelevanceResult.model_validate(r) for r in _read(results_path)]
                browser_wait = False
                for result in results:
                    if result.status != "completed" and result.error_code == "ZINIAO_CDP_UNAVAILABLE":
                        result.status, result.attempts = "waiting", 0
                        browser_wait = True
                if browser_wait:
                    _write(results_path, [r.model_dump(mode="json") for r in results])
                    job.failed = sum(r.status == "failed" for r in results)
                interrupted = job.status in ("running", "queued") or (
                    job.status == "failed" and "服务重启中断核验" in job.error
                )
                if interrupted or browser_wait:
                    job.status, job.phase = "queued", "waiting"
                    job.current_keyword = ""
                    job.error = "正在自动恢复未完成核验，已完成结果保留"
                    job.next_retry_at = None
                    self._save(job)
        if self.auto_execute and self._scheduler is None:
            def schedule() -> None:
                while not self._stop.is_set():
                    self._tick()
                    self._stop.wait(15)
            self._scheduler = Thread(target=schedule, name="excel-recovery", daemon=True)
            self._scheduler.start()

    def close(self) -> None:
        self._stop.set()
        if self._scheduler is not None:
            self._scheduler.join(timeout=2)
        self._model_pool.shutdown(wait=False, cancel_futures=True)
        if self._owns_executor:
            self.executor.shutdown(wait=False, cancel_futures=True)

    def _reuse_collections(self, job: RelevanceExport) -> dict[str, CollectionResult]:
        """Only same-run, same-profile, recent query-specific search evidence is eligible."""
        cutoff = datetime.now(UTC) - timedelta(hours=24)
        reuse: dict[str, CollectionResult] = {}
        with self.repository.connect() as conn:
            for table in ("product_term_analyses", "hot_search_core_analyses"):
                rows = conn.execute(f"SELECT document FROM {table} WHERE run_id=? ORDER BY seq DESC LIMIT 3",
                                    (str(job.run_id),)).fetchall()
                for row in rows:
                    doc = json.loads(row[0])
                    if doc.get("profile_version") != job.profile_version:
                        continue
                    captured = datetime.fromisoformat(doc["created_at"].replace("Z", "+00:00"))
                    if captured < cutoff:
                        continue
                    if table == "hot_search_core_analyses" and (
                        doc.get("marketplace") != job.marketplace or doc.get("delivery_region") != job.delivery_region
                        or doc.get("profile_snapshot") != job.profile.model_dump(mode="json")
                    ):
                        continue
                    if table == "product_term_analyses":
                        candidates = [(i.get("text", ""), i.get("collection")) for i in doc.get("items", [])]
                    else:
                        names = {str(i["candidate_id"]): i["keyword"] for i in doc.get("items", [])}
                        collections = dict(doc.get("collections", {}))
                        journals = conn.execute(
                            "SELECT document FROM hot_search_core_analysis_journal WHERE analysis_id=? ORDER BY seq",
                            (doc["id"],),
                        ).fetchall()
                        for journal in journals:
                            checkpoint = json.loads(journal[0]).get("checkpoint", {})
                            if checkpoint.get("collection") is not None:
                                collections[str(checkpoint["candidate_id"])] = checkpoint["collection"]
                        candidates = [(names.get(k, ""), v) for k, v in collections.items()]
                    for keyword, raw in candidates:
                        if not keyword or not raw or fold_text(keyword) in reuse:
                            continue
                        collection = CollectionResult.model_validate(raw)
                        if fold_text(collection.term) != fold_text(keyword):
                            continue
                        if not collection.pages or any(
                            job.delivery_region not in p.region for p in collection.pages
                        ):
                            continue
                        if any(p.captured_at < cutoff for p in collection.products):
                            continue
                        reuse[fold_text(keyword)] = collection
        return reuse

    def _collect(self, job: RelevanceExport, keyword: str, evidence: Path,
                 reusable: dict[str, CollectionResult],
                 snapshots: dict[str, ProductSnapshot] | None = None,
                 refresh_details: bool = False) -> CollectionResult:
        snapshots = snapshots if snapshots is not None else {}
        def remember(product: ProductSnapshot) -> ProductSnapshot:
            snapshots[product.asin] = product
            return product
        class CachedSession:
            def __init__(self, raw: ProductSearchSession) -> None:
                self.raw = raw
            def search(self, keyword: str, page_number: int, evidence_name: str) -> SearchPage:
                return self.raw.search(keyword, page_number, evidence_name)
            def product(self, asin: str) -> ProductSnapshot:
                product = snapshots.get(asin)
                if product is not None and product.captured_at >= datetime.now(UTC) - timedelta(hours=24):
                    return product
                return remember(self.raw.product(asin))
            def products(self, asins: list[str]) -> list[tuple[str, ProductSnapshot | None, Exception | None]]:
                cached = {a: snapshots[a] for a in asins if a in snapshots
                          and snapshots[a].captured_at >= datetime.now(UTC) - timedelta(hours=24)}
                missing = [a for a in asins if a not in cached]
                outcomes: dict[str, tuple[str, ProductSnapshot | None, Exception | None]] = {
                    a: (a, p, None) for a, p in cached.items()
                }
                fetch_many = getattr(self.raw, "products", None)
                if missing and callable(fetch_many):
                    returned = cast(list[tuple[str, ProductSnapshot | None, Exception | None]], fetch_many(missing))
                    for asin, product, error in returned:
                        if product is not None and error is None and product.asin == asin:
                            remember(product)
                        outcomes[asin] = (asin, product, error)
                else:
                    for asin in missing:
                        try:
                            outcomes[asin] = (asin, self.product(asin), None)
                        except Exception as error:
                            outcomes[asin] = (asin, None, error)
                return [outcomes.get(a, (a, None, DomainError(502, "PRODUCT_DETAILS_MISSING", "商品详情未返回"))) for a in asins]
        saved = evidence / "collection.json"
        collection = CollectionResult.model_validate(_read(saved)) if saved.exists() else reusable.get(fold_text(keyword))
        if collection is not None:
            # Keep the original natural sample; retry only details missing from that sample.
            collection = collection.model_copy(deep=True)
            collection.unique_asins = list(dict.fromkeys(collection.unique_asins))[:SAMPLE_LIMIT]
            if refresh_details:
                collection.products = []
                for asin in collection.unique_asins:
                    snapshots.pop(asin, None)
            by_asin = {p.asin: p for p in collection.products}
            collection.products = [by_asin[a] for a in collection.unique_asins if a in by_asin]
            missing = [a for a in collection.unique_asins if a not in {p.asin for p in collection.products}]
            if missing:
                with self.search.open(job.marketplace, evidence) as raw:
                    session = CachedSession(cast(ProductSearchSession, raw))
                    for asin, product, detail_error in session.products(missing):
                        try:
                            if detail_error is not None:
                                raise detail_error
                            if product is None:
                                raise DomainError(502, "PRODUCT_DETAILS_MISSING", "商品详情未返回")
                            if product.asin != asin:
                                raise DomainError(502, "ASIN_IDENTITY_UNCONFIRMED", "商品身份不符")
                            collection.products.append(product)
                            collection.failures = [f for f in collection.failures if f.asin != asin]
                        except Exception as error:
                            if isinstance(error, DomainError) and error.code in EXCEL_PAUSE_CODES:
                                _write(saved, collection.model_dump(mode="json"))
                                raise
                            log.info("relevance detail retry %s: %s", asin, type(error).__name__)
                            collection.failures = [f for f in collection.failures if f.asin != asin]
                            collection.failures.append(ProductCollectionFailure(
                                asin=asin, reason=error.message if isinstance(error, DomainError) else "商品详情采集失败",
                                error_code=error.code if isinstance(error, DomainError) else "PRODUCT_DETAILS_FAILED",
                            ))
            _write(saved, collection.model_dump(mode="json"))
            return collection
        with self.search.open(job.marketplace, evidence) as raw:
            collection = collect_natural_products(CachedSession(cast(ProductSearchSession, raw)), keyword, SAMPLE_LIMIT, "relevance")
        _write(saved, collection.model_dump(mode="json"))
        return collection

    def _cache_key(self, job: RelevanceExport, product: ProductSnapshot) -> str:
        # Timestamps and image/source URLs are not inputs to the text comparison or scorer.
        return _digest({"product": product.model_dump(mode="json", exclude={"captured_at", "image_url", "source_url"}),
                        "profile": job.profile.model_dump(mode="json"), "model": job.model,
                        "endpoint": job.model_endpoint, "protocol": job.model_protocol,
                        "marketplace": job.marketplace, "region": job.delivery_region,
                        "rule": RULE_VERSION, "comparison": COMPARISON_VERSION})

    def _cache(self, job: RelevanceExport) -> Path:
        return self.root / str(job.project_id) / str(job.run_id) / ".comparisons"

    def _seed_cache(self, job: RelevanceExport, snapshots: dict[str, ProductSnapshot]) -> None:
        """Import compatible prior Excel evidence, including the interrupted deployment's batches."""
        cutoff = datetime.now(UTC) - timedelta(hours=24)
        for manifest in (self.root / str(job.project_id) / str(job.run_id)).glob("*/manifest.json"):
            old = RelevanceExport.model_validate(_read(manifest))
            if (old.profile != job.profile or old.rule_version != job.rule_version
                or (old.model, old.model_endpoint, old.model_protocol, old.marketplace, old.delivery_region)
                   != (job.model, job.model_endpoint, job.model_protocol, job.marketplace, job.delivery_region)):
                continue
            for saved in (manifest.parent / "evidence").glob("*/collection.json"):
                collection = CollectionResult.model_validate(_read(saved))
                for product in collection.products:
                    if product.captured_at < cutoff:
                        continue
                    snapshots.setdefault(product.asin, product)
                    legacy_key = _digest({"product": product.model_dump(mode="json"), "profile": old.profile.model_dump(mode="json"),
                                          "model": old.model, "rule": RULE_VERSION, "comparison": COMPARISON_VERSION})
                    legacy = manifest.parent / "evidence" / "comparisons" / (legacy_key + ".json")
                    target = self._cache(job) / (self._cache_key(job, product) + ".json")
                    if legacy.exists() and not target.exists() and datetime.fromtimestamp(legacy.stat().st_mtime, UTC) >= cutoff:
                        _write(target, _read(legacy))

    def _compare_batch(self, job: RelevanceExport, batch: list[tuple[ProductSnapshot, str]],
                       client: QwenClient, evidence: Path) -> list[dict[str, Any]]:
        products = [p for p, _ in batch]
        call = evidence / ("model-call-" + _digest([k for _, k in batch]) + ".json")
        started = monotonic()
        _write(call, {"status": "pending", "asins": [p.asin for p in products]})
        try:
            compared = client.compare_product_batch(job.profile, products)
            mapping = {d.asin: d for d in compared.items}
            if len(mapping) != len(products) or set(mapping) != {p.asin for p in products}:
                raise DomainError(502, "MODEL_OUTPUT_INVALID", "商品比较结果未覆盖请求样本")
            unresolved = [p for p in products if not mapping[p.asin].valid]
            if any(mapping[p.asin].exclusion_code.startswith("MODEL_") or
                   mapping[p.asin].exclusion_code == "OUTCOME_UNKNOWN" for p in unresolved):
                raise DomainError(502, "MODEL_OUTPUT_INVALID", "商品模型核验失败，将自动补核验")
            judgments = {j.asin: j for j in classify_unscorable(client, job.profile, unresolved).items} if unresolved else {}
            outcomes = []
            for product, key in batch:
                draft = mapping[product.asin]
                outcome: dict[str, Any] = {"asin": product.asin, "valid": False, "high": False}
                if not draft.valid:
                    judgment = judgments[product.asin]
                    outcome.update(valid=judgment.verdict == "unrelated", reason=judgment.reason,
                                   verdict=judgment.verdict)
                elif draft.scores is not None:
                    candidate = ProductProfile(
                        asin=product.asin, purpose=draft.purpose, style_structure=draft.style_structure,
                        specifications=draft.specifications, package_quantity=draft.package_quantity,
                        unit_basis=draft.unit_basis, price=product.price if product.price_reliable else None,
                        currency=product.currency if product.price_reliable else None,
                    )
                    comparison = score_product(job.profile, candidate, draft.scores)
                    outcome.update(valid=comparison.status != "excluded", high=comparison.high_related,
                                   comparison=comparison.model_dump(mode="json"))
                else:
                    raise DomainError(502, "MODEL_OUTPUT_INVALID", "商品模型核验缺少维度分数")
                _write(self._cache(job) / (key + ".json"), outcome)
                outcomes.append(outcome)
            _write(call, {"status": "completed", "asins": [p.asin for p in products], "seconds": monotonic()-started})
            return outcomes
        except Exception as error:
            _write(call, {"status": "failed", "error_code": error.code if isinstance(error, DomainError) else "MODEL_ERROR",
                          "seconds": monotonic()-started})
            raise

    def _verify(self, job: RelevanceExport, result: RelevanceResult,
                reusable: dict[str, CollectionResult], client: QwenClient,
                snapshots: dict[str, ProductSnapshot] | None = None) -> None:
        directory = self._directory(job.project_id, job.run_id, job.id)
        evidence = directory / "evidence" / _digest(result.keyword)
        job.phase = "collecting"
        self._save(job)
        started = monotonic()
        refresh_details = result.attempts > 1 and result.error_code == "RELEVANCE_NO_VALID_SAMPLES"
        try:
            collection = self._collect(job, result.keyword, evidence, reusable, snapshots, refresh_details)
        finally:
            result.collection_seconds += monotonic() - started
        # Count each requested natural ASIN once; unmatched/duplicated details cannot inflate the denominator.
        collection.unique_asins = list(dict.fromkeys(collection.unique_asins))[:SAMPLE_LIMIT]
        products = {p.asin: p for p in collection.products if p.asin in collection.unique_asins}
        collection.products = [products[a] for a in collection.unique_asins if a in products]
        missing = set(collection.unique_asins) - products.keys()
        recorded = {f.asin for f in collection.failures}
        collection.failures.extend(ProductCollectionFailure(
            asin=a, reason="未取得身份匹配的商品详情", error_code="PRODUCT_DETAILS_MISSING",
        ) for a in collection.unique_asins if a in missing - recorded)
        _write(evidence / "collection.json", collection.model_dump(mode="json"))
        # The shared detail collector records some browser errors per ASIN.
        # In Excel these mean wait for the browser, rather than score partial data.
        for failure in collection.failures:
            if failure.error_code in EXCEL_PAUSE_CODES:
                raise DomainError(503, failure.error_code, failure.reason)
        result.sampled = len(collection.unique_asins)
        result.failures = [f"{f.asin}: {f.reason}" for f in collection.failures]
        job.phase = "verifying"
        self._save(job)
        outcomes: list[dict[str, Any]] = []
        cache = self._cache(job)
        pending: list[tuple[ProductSnapshot, str]] = []
        for product in collection.products:
            key = self._cache_key(job, product)
            path = cache / (key + ".json")
            if (not refresh_details and path.exists()
                and datetime.fromtimestamp(path.stat().st_mtime, UTC) >= datetime.now(UTC) - timedelta(hours=24)):
                outcomes.append(_read(path))
            else:
                pending.append((product, key))
        started = monotonic()
        futures = [self._model_pool.submit(self._compare_batch, job, pending[offset:offset+10], client, evidence)
                   for offset in range(0, len(pending), 10)]
        errors = []
        try:
            for future in as_completed(futures):
                try:
                    outcomes.extend(future.result())
                except Exception as error:
                    errors.append(error)
            if errors:
                raise errors[0]
        finally:
            result.model_seconds += monotonic() - started
        _write(evidence / "outcomes.json", outcomes)
        result.valid_samples = sum(bool(o["valid"]) for o in outcomes)
        result.high_related_count = sum(bool(o["valid"] and o["high"]) for o in outcomes)
        result.failures.extend(f"{o['asin']}: {o.get('reason', '证据不足')}" for o in outcomes if not o["valid"])
        if not result.valid_samples:
            raise DomainError(422, "RELEVANCE_NO_VALID_SAMPLES", "没有有效核验商品，请补采或重试；未按0%计算")
        result.ratio = Decimal(result.high_related_count) / Decimal(result.valid_samples)
        result.status, result.error = "completed", ""

    def execute(self, job: RelevanceExport) -> None:
        directory = self._directory(job.project_id, job.run_id, job.id)
        results = [RelevanceResult.model_validate(r) for r in _read(directory / "results.json")]
        try:
            with browser_device_context(job.device_id), browser_operation_context(str(job.id)):
                if self._stop.is_set():
                    return
                if self.collector_ready is not None and not self.collector_ready():
                    job.status, job.phase = "failed", "waiting"
                    job.error = "等待采集服务恢复，恢复后自动继续；已完成结果保留"
                    job.next_retry_at = datetime.now(UTC) + timedelta(seconds=30)
                    return
                job.status = "running"
                job.error, job.next_retry_at = "", None
                self._save(job)
                client = self._model()
                if (client.config.model, client.config.base_url, client.config.protocol) != (
                    job.model, job.model_endpoint, job.model_protocol
                ):
                    raise DomainError(409, "RELEVANCE_MODEL_CHANGED", "模型已变化，请重新创建核验任务")
                if os.getenv("KEYWORD_US_DELIVERY_ZIP", "10001").strip() != job.delivery_region:
                    raise DomainError(409, "RELEVANCE_REGION_CHANGED", "配送地区已变化，请重新创建核验任务")
                reusable = self._reuse_collections(job)
                snapshots = {p.asin: p for c in reusable.values() for p in c.products}
                self._seed_cache(job, snapshots)
                paused = False
                for result in results:
                    if self._stop.is_set():
                        job.status, job.phase = "queued", "waiting"
                        job.error = "服务停止中，未完成核验将在启动后自动继续"
                        return
                    if result.status == "completed" or result.attempts >= 3:
                        continue
                    job.current_keyword = result.keyword
                    self._save(job)
                    result.status = "running"
                    result.attempts += 1
                    _write(directory / "results.json", [r.model_dump(mode="json") for r in results])
                    try:
                        self._verify(job, result, reusable, client, snapshots)
                        result.error_code = ""
                    except DomainError as error:
                        result.status, result.error = "failed", error.message
                        result.error_code = error.code
                        if error.code in EXCEL_PAUSE_CODES:
                            # Connectivity waits do not consume the model retry budget.
                            result.status = "waiting"
                            result.attempts -= 1
                            paused = True
                        elif error.code in {"MODEL_NOT_CONFIGURED", "MODEL_CONFIG_INVALID", "MODEL_PROVIDER_ERROR_AUTH"}:
                            result.attempts = 3
                    except Exception:
                        log.exception("Excel keyword verification failed: %s", result.keyword)
                        result.status, result.error_code = "failed", "VERIFICATION_ERROR"
                        result.error = "核验临时异常，将自动重试未完成项"
                    _write(directory / "results.json", [r.model_dump(mode="json") for r in results])
                    job.completed = sum(r.status == "completed" for r in results)
                    job.failed = sum(r.status == "failed" for r in results)
                    job.missing_samples = sum(max(0, SAMPLE_LIMIT - r.valid_samples) for r in results
                                              if r.status in ("completed", "failed"))
                    self._save(job)
                    if paused:
                        break
                if job.completed == job.total:
                    job.phase = "exporting"
                    self._save(job)
                    report = LibraryReport.model_validate(_read(directory / "evidence" / "library.json"))
                    data = export_xlsx(report, self.library.template_roots(report), ranked_only=job.ranked_only,
                                       relevance_results={r.keyword: r for r in results})
                    temporary = directory / "final.xlsx.tmp"
                    temporary.write_bytes(data)
                    temporary.replace(directory / "final.xlsx")
                    job.status, job.error, job.next_retry_at = "completed", "", None
                else:
                    retryable = [r for r in results if r.status != "completed" and r.attempts < 3]
                    job.status = "failed"
                    reason = next((r.error for r in results if r.error), "核验尚未完成")
                    if retryable:
                        delay = 30 if paused else (30 if max(r.attempts for r in retryable) <= 1 else 120)
                        job.next_retry_at = datetime.now(UTC) + timedelta(seconds=delay)
                        job.error = f"{reason}；将自动继续未完成核验，已完成结果保留"
                    else:
                        job.next_retry_at = None
                        job.error = f"自动重试已达上限，仍未完成：{reason}；请检查采集或模型服务"
        except Exception as error:
            log.exception("library relevance job failed: %s", job.id)
            job.status = "failed"
            job.error = error.message if isinstance(error, DomainError) else "核验中断，请检查采集设备和模型配置后重试"
            job.next_retry_at = None
        finally:
            job.current_keyword = ""
            job.phase = "waiting" if job.next_retry_at else ""
            self._save(job)

    def download(self, project: UUID, run: UUID, library: UUID, key: UUID) -> bytes:
        job = self.get(project, run, library, key)
        if job.status != "completed":
            raise DomainError(409, "RELEVANCE_NOT_COMPLETE", "相关性核验未完成，请先完成或重试")
        directory = self._directory(project, run, key)
        saved = directory / "final.xlsx"
        frozen = LibraryReport.model_validate(_read(directory / "evidence" / "library.json"))
        report = self.library.get(project, run, library)
        if not self._matches(job, report, job.ranked_only, self._identity(report, job.ranked_only)):
            report = frozen
        export_key = _digest(report.model_dump(mode="json", exclude={"stale"}))
        frozen_key = _digest(frozen.model_dump(mode="json", exclude={"stale"}))
        if export_key == frozen_key and saved.exists():
            return saved.read_bytes()
        refreshed = directory / "exports" / (export_key + ".xlsx")
        if refreshed.exists():
            return refreshed.read_bytes()
        results = [RelevanceResult.model_validate(r) for r in _read(directory / "results.json")]
        if len(results) != job.total or any(r.status != "completed" or r.ratio is None for r in results):
            raise DomainError(409, "RELEVANCE_NOT_COMPLETE", "相关性结果不完整，请重新核验")
        data = export_xlsx(report, self.library.template_roots(report), ranked_only=job.ranked_only,
                           relevance_results={r.keyword: r for r in results})
        refreshed.parent.mkdir(parents=True, exist_ok=True)
        temporary = refreshed.with_name(uuid4().hex + ".tmp")
        temporary.write_bytes(data)
        temporary.replace(refreshed)
        return data
