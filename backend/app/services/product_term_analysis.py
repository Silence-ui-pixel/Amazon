"""商品词相关度分析：采集、批量比较、纯函数计分与自动择优。"""

import hashlib
import json
import os
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from typing import Literal, cast
from uuid import UUID, uuid4

from app.db.repository import Repository
from app.domain.asin_product import ProductSnapshot
from app.domain.errors import DomainError
from app.domain.product_terms import (
    BatchComparisonSet,
    ProductComparison,
    ProductProfile,
    ProductTermAnalysis,
    TermAnalysisItem,
)
from app.domain.roots import RootSelection
from app.domain.verification import SearchPage
from app.services.amazon_search import SearchProvider
from app.services.product_comparison_semantics import COMPARISON_VERSION
from app.services.product_profiles import ProductProfileService
from app.services.product_term_collection import (
    SAMPLE_LIMIT,
    ProductSearchSession,
    collect_natural_products,
)
from app.services.product_term_scoring import choose_terms, score_product, summarize_term
from app.services.qwen import QwenClient, QwenConfig
from app.services.roots import RootService
from app.services.search_policy import PAUSE_CODES


def _relevance_model_concurrency() -> int:
    """Limit paid model fan-out even when the environment is misconfigured."""
    try:
        configured = int(os.environ.get("KEYWORD_RELEVANCE_MODEL_CONCURRENCY", "3") or "3")
    except ValueError:
        configured = 3
    return max(1, min(3, configured))


def _compare_with_format_fallback(
    model: QwenClient,
    reference: ProductProfile,
    products: list[ProductSnapshot],
) -> BatchComparisonSet:
    """Split a malformed large response once; small malformed batches fail explicitly."""
    try:
        return model.compare_product_batch(reference, products)
    except DomainError as error:
        if error.code != "MODEL_OUTPUT_INVALID" or len(products) <= 5:
            raise
    items = []
    for offset in range(0, len(products), 5):
        items.extend(model.compare_product_batch(reference, products[offset:offset + 5]).items)
    return BatchComparisonSet(items=items)


class ProductTermAnalysisService:
    def __init__(self, repository: Repository, roots: RootService, profiles: ProductProfileService,
                 search: SearchProvider, client: QwenClient | None) -> None:
        self.repository = repository
        self.roots = roots
        self.profiles = profiles
        self.search = search
        self.client = client

    def get(self, project: UUID, run: UUID, analysis_id: UUID | None = None) -> ProductTermAnalysis:
        self.repository.get_run(project, run)
        with self.repository.connect() as conn:
            row = conn.execute(
                "SELECT document FROM product_term_analyses WHERE run_id=? "
                + ("AND id=?" if analysis_id else "ORDER BY seq DESC LIMIT 1"),
                (str(run), str(analysis_id)) if analysis_id else (str(run),),
            ).fetchone()
        if not row:
            raise DomainError(404, "PRODUCT_TERM_ANALYSIS_NOT_FOUND", "尚无商品词相关度分析")
        result = ProductTermAnalysis.model_validate_json(row[0])
        if analysis_id is None:
            roots = self.roots.get(project, run)
            if result.candidate_version != roots.version or {i.candidate_id for i in result.items} != {c.id for c in roots.candidates}:
                raise DomainError(404, "PRODUCT_TERM_ANALYSIS_NOT_FOUND", "当前版本尚未分析；历史结果仍保留")
        return result

    def history(self, project: UUID, run: UUID) -> list[ProductTermAnalysis]:
        self.repository.get_run(project, run)
        with self.repository.connect() as conn:
            rows = conn.execute("SELECT document FROM product_term_analyses WHERE run_id=? ORDER BY seq DESC", (str(run),)).fetchall()
        return [ProductTermAnalysis.model_validate_json(row[0]) for row in rows]

    def recover(self) -> None:
        """重启时不重发可能已计费的调用；把未决候选标为失败并保留已完成证据。"""
        with self.repository.connect() as conn:
            rows = conn.execute(
                "SELECT id, fingerprint, document FROM product_term_analyses"
            ).fetchall()
            for analysis_id, _fingerprint, document in rows:
                analysis = ProductTermAnalysis.model_validate_json(document)
                if analysis.status not in ("queued", "running"):
                    continue
                for item in analysis.items:
                    if item.status in ("waiting", "running"):
                        item.status = "failed"
                        item.error = "服务重启时模型或采集结果未知，未自动重试"
                analysis.complete = False
                analysis.status = "incomplete"
                analysis.best_candidate_ids = choose_terms(analysis.items)
                if analysis.best_candidate_ids:
                    existing = conn.execute(
                        "SELECT document FROM root_selections WHERE run_id=?",
                        (str(analysis.run_id),),
                    ).fetchone()
                    if not existing or RootSelection.model_validate_json(existing[0]).source != "manual":
                        selection = RootSelection(
                            project_id=analysis.project_id,
                            run_id=analysis.run_id,
                            candidate_ids=analysis.best_candidate_ids,
                            confirmed_at=datetime.now(UTC),
                            source="automatic",
                            verification_id=analysis.id,
                        )
                        conn.execute(
                            "INSERT INTO root_selections VALUES(?,?) ON CONFLICT(run_id) DO UPDATE SET document=excluded.document",
                            (str(analysis.run_id), selection.model_dump_json()),
                        )
                conn.execute(
                    "UPDATE product_term_analyses SET document=? WHERE id=?",
                    (analysis.model_dump_json(), analysis_id),
                )

    def _save(self, analysis: ProductTermAnalysis, fingerprint: str) -> None:
        with self.repository.connect() as conn:
            conn.execute(
                "INSERT INTO product_term_analyses (id, run_id, fingerprint, document) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET document=excluded.document",
                (str(analysis.id), str(analysis.run_id), fingerprint, analysis.model_dump_json()),
            )

    def _cached(self, run: UUID, fingerprint: str) -> ProductTermAnalysis | None:
        with self.repository.connect() as conn:
            row = conn.execute(
                "SELECT document FROM product_term_analyses WHERE run_id=? AND (fingerprint=? OR fingerprint LIKE ?) "
                "ORDER BY seq DESC LIMIT 1",
                (str(run), fingerprint, fingerprint + ":%"),
            ).fetchone()
        return ProductTermAnalysis.model_validate_json(row[0]) if row else None

    def _apply_current_selection_rule(
        self, analysis: ProductTermAnalysis, fingerprint: str
    ) -> ProductTermAnalysis:
        """用已保存证据更新自动选择，不重新采集或覆盖人工选择。"""
        winners = choose_terms(analysis.items)
        analysis.best_candidate_ids = winners
        analysis.status = "completed" if winners else "awaiting_review"
        self._annotate(analysis)
        with self.repository.connect() as conn:
            existing = conn.execute(
                "SELECT document FROM root_selections WHERE run_id=?",
                (str(analysis.run_id),),
            ).fetchone()
            selection = RootSelection.model_validate_json(existing[0]) if existing else None
            if selection is None or selection.source != "manual":
                if winners:
                    current = RootSelection(
                        project_id=analysis.project_id,
                        run_id=analysis.run_id,
                        candidate_ids=winners,
                        confirmed_at=datetime.now(UTC),
                        source="automatic",
                        verification_id=analysis.id,
                    )
                    conn.execute(
                        "INSERT INTO root_selections VALUES(?,?) "
                        "ON CONFLICT(run_id) DO UPDATE SET document=excluded.document",
                        (str(analysis.run_id), current.model_dump_json()),
                    )
                elif selection is not None:
                    conn.execute(
                        "DELETE FROM root_selections WHERE run_id=?",
                        (str(analysis.run_id),),
                    )
            conn.execute(
                "UPDATE product_term_analyses SET document=? WHERE id=?",
                (analysis.model_dump_json(), str(analysis.id)),
            )
        return analysis

    def start(
        self, project: UUID, run: UUID, *, refresh: bool = False
    ) -> ProductTermAnalysis:
        analysis, fingerprint, claimed = self._prepare(project, run, refresh=refresh)
        if not claimed:
            if analysis.status in ("queued", "running"):
                raise DomainError(409, "PRODUCT_TERM_ANALYSIS_RUNNING", "已有商品词分析正在进行，请等待真实进度更新")
            return analysis
        try:
            return self._execute(project, run, analysis, fingerprint)
        except Exception:
            analysis.status = "incomplete"
            analysis.complete = False
            self._save(analysis, fingerprint)
            raise

    def enqueue(self, project: UUID, run: UUID, *, refresh: bool = False) -> ProductTermAnalysis:
        analysis, _, _ = self._prepare(project, run, refresh=refresh, queued=True)
        return analysis

    def execute_queued(self, project: UUID, run: UUID, analysis_id: UUID) -> None:
        with self.repository.connect() as conn:
            conn.execute("SELECT id FROM pipeline_runs WHERE id=? FOR NO KEY UPDATE", (str(run),))
            row = conn.execute("SELECT fingerprint,document FROM product_term_analyses WHERE run_id=? AND id=? FOR UPDATE", (str(run), str(analysis_id))).fetchone()
            if not row:
                return
            analysis = ProductTermAnalysis.model_validate_json(row[1])
            if analysis.status != "queued":
                return
            analysis.status = "running"
            conn.execute("UPDATE product_term_analyses SET document=? WHERE id=?", (analysis.model_dump_json(), str(analysis.id)))
        try:
            self._execute(project, run, analysis, row[0])
        except Exception as error:
            # 后台异常也必须落盘，不能让页面永久显示正在运行。
            analysis.status = "incomplete"
            analysis.complete = False
            for item in analysis.items:
                if item.status in ("waiting", "running"):
                    item.status = "failed"
                    item.phase = "failed"
                    item.error = error.message if isinstance(error, DomainError) else "分析执行中断，请检查采集服务后继续未完成候选"
            self._annotate(analysis)
            self._save(analysis, row[0])

    def _prepare(self, project: UUID, run: UUID, *, refresh: bool = False, queued: bool = False) -> tuple[ProductTermAnalysis, str, bool]:
        with self.repository.connect() as conn:
            conn.execute("SELECT id FROM pipeline_runs WHERE id=? FOR NO KEY UPDATE", (str(run),))
            root_state = conn.execute("SELECT status FROM root_candidates WHERE run_id=?", (str(run),)).fetchone()
            if root_state and root_state[0] != "succeeded":
                raise DomainError(409, "ROOT_GENERATION_PENDING", "请等待候选生成完成后分析")
            active = conn.execute("SELECT fingerprint,document FROM product_term_analyses WHERE run_id=? AND document::jsonb->>'status' IN ('queued','running') ORDER BY seq DESC LIMIT 1", (str(run),)).fetchone()
            if active:
                return ProductTermAnalysis.model_validate_json(active[1]), active[0], False
            analysis, fingerprint, claimed = self._prepare_locked(project, run, refresh=refresh, queued=queued)
            return analysis, fingerprint, claimed

    def _prepare_locked(self, project: UUID, run: UUID, *, refresh: bool, queued: bool) -> tuple[ProductTermAnalysis, str, bool]:
        profile_record = self.profiles.get(project, run)
        if profile_record.status != "confirmed":
            raise DomainError(409, "PRODUCT_PROFILE_REQUIRED", "请先确认商品四维基准")
        candidates = self.roots.get(project, run)
        if candidates.rule_version != "product-term-v1":
            raise DomainError(409, "PRODUCT_TERM_CANDIDATES_REQUIRED", "旧词根任务不能启动新商品词分析")
        fingerprint = hashlib.sha256(
            json.dumps(
                {"profile": profile_record.version, "candidates": [str(c.id) for c in candidates.candidates],
                 "marketplace": candidates.marketplace, "limit": SAMPLE_LIMIT, "rule": candidates.rule_version, "comparison_version": COMPARISON_VERSION,
                 **({"candidate_version": candidates.version} if candidates.version > 1 else {})},
                sort_keys=True,
            ).encode()
        ).hexdigest()
        cached = self._cached(run, fingerprint)
        if cached is not None and cached.complete and not refresh:
            # 只有完整结果可以复用；中断或空结果必须重新采集，否则重试会永远撞上旧结果。
            return self._apply_current_selection_rule(cached, fingerprint), fingerprint, False
        # 显式重采创建新分析，保留旧证据；普通继续仅补跑未完成候选。
        if refresh:
            fingerprint += ":" + str(uuid4())
        if cached is None or refresh:
            analysis = ProductTermAnalysis(
                id=uuid4(), project_id=project, run_id=run, profile_version=profile_record.version,
                candidate_version=candidates.version,
                comparison_version=COMPARISON_VERSION,
                status="running", items=[TermAnalysisItem(candidate_id=c.id, text=c.text, status="waiting") for c in candidates.candidates]
            )
            if candidates.version > 1 and not refresh:
                reusable = {}
                for previous in self.history(project, run):
                    if previous.profile_version != profile_record.version or previous.comparison_version != COMPARISON_VERSION:
                        continue
                    for item in previous.items:
                        if item.status in ("completed", "no_samples") and item.candidate_id not in reusable:
                            reusable[item.candidate_id] = item
                analysis.items = [reusable[item.candidate_id].model_copy(deep=True) if item.candidate_id in reusable else item for item in analysis.items]
        else:
            analysis = cached
            for item in analysis.items:
                if not refresh and item.status in ("completed", "no_samples"):
                    continue
                item.status = "waiting"
                item.error = ""
                if refresh:
                    item.summary = None
                    item.comparisons = []
                    item.collection = None
            analysis.status = "running"
            analysis.complete = False
        analysis.status = "queued" if queued else "running"
        if not analysis.items:
            analysis.status = "awaiting_review"
            analysis.complete = True
        self._save(analysis, fingerprint)
        return analysis, fingerprint, bool(analysis.items)

    @staticmethod
    def _annotate(analysis: ProductTermAnalysis) -> None:
        for item in analysis.items:
            before = (item.phase, item.rejection_reason)
            if item.status == "failed":
                item.phase = "failed"
                item.rejection_reason = "分析失败，未按0%计算：" + item.error
            elif item.status in ("completed", "no_samples"):
                item.phase = "completed"
                if not item.summary or not item.summary.valid_samples:
                    item.rejection_reason = "没有有效评分样本，请展开商品剔除原因"
                elif item.summary.relevance_percent is None or item.summary.relevance_percent <= 40:
                    item.rejection_reason = f"高相关商品 {item.summary.high_related_count}/{item.summary.valid_samples}，比例 {item.summary.relevance_percent}%，未严格大于40%"
                else:
                    item.rejection_reason = ""
            if before != (item.phase, item.rejection_reason):
                item.updated_at = datetime.now(UTC)

    def _execute(self, project: UUID, run: UUID, analysis: ProductTermAnalysis, fingerprint: str) -> ProductTermAnalysis:
        profile_record = self.profiles.get(project, run)
        if all(item.status in ("completed", "no_samples") for item in analysis.items):
            analysis.complete = True
            return self._apply_current_selection_rule(analysis, fingerprint)
        model = self.client or QwenClient(
            QwenConfig.load(self.repository.storage_root),
            diagnostic_dir=self.repository.storage_root / "model_diagnostics",
        )
        model.config.validate()
        run_data = self.repository.get_run(project, run)
        evidence = self.repository.storage_root / "evidence" / str(analysis.id)
        cache: dict[str, ProductSnapshot] = {}

        class CachedSession:
            def __init__(self, session: ProductSearchSession) -> None:
                self.session = session

            def search(self, keyword: str, page_number: int, evidence_name: str) -> SearchPage:
                return self.session.search(keyword, page_number, evidence_name)

            def product(self, asin: str) -> ProductSnapshot:
                if asin not in cache:
                    cache[asin] = self.session.product(asin)
                return cache[asin]

            def products(self, asins: list[str]) -> list[tuple[str, ProductSnapshot | None, Exception | None]]:
                missing = [asin for asin in asins if asin not in cache]
                fetch_many = getattr(self.session, "products", None)
                if missing and callable(fetch_many):
                    fetched = fetch_many(missing)
                    for asin, product, error in fetched:
                        if product is not None and error is None:
                            cache[asin] = product
                    by_asin = {asin: (product, error) for asin, product, error in fetched}
                    return [
                        (asin, cache.get(asin, by_asin.get(asin, (None, None))[0]), by_asin.get(asin, (None, None))[1])
                        for asin in asins
                    ]
                results = []
                for asin in asins:
                    if asin in cache:
                        results.append((asin, cache[asin], None))
                        continue
                    try:
                        product = self.session.product(asin)
                        cache[asin] = product
                        results.append((asin, product, None))
                    except Exception as error:
                        results.append((asin, None, error))
                return results
        with self.search.open(run_data.input.marketplace, evidence) as raw_session:
            session = CachedSession(cast(ProductSearchSession, raw_session))
            for item in analysis.items:
                if item.status in ("completed", "no_samples"):
                    continue
                item.status = "running"
                item.phase = "searching"
                item.error = ""
                item.rejection_reason = ""
                item.comparisons = []
                item.summary = None
                self._save(analysis, fingerprint)
                try:
                    def progress(phase: Literal["searching", "collecting"], pages: int, found: int, collected: int, failed: int, item: TermAnalysisItem = item) -> None:
                        item.phase = phase
                        item.search_pages = pages
                        item.found_products = found
                        item.collected_products = collected
                        item.collection_failures = failed
                        item.updated_at = datetime.now(UTC)
                        self._save(analysis, fingerprint)

                    collected = collect_natural_products(
                        session, item.text, SAMPLE_LIMIT, f"candidate-{item.candidate_id}", progress=progress
                    )
                    item.collection = collected
                    comparisons: list[ProductComparison] = []
                    positions = {
                        asin: position for position, asin in enumerate(collected.unique_asins, 1)
                    }
                    for failure in collected.failures:
                        comparisons.append(
                            ProductComparison(
                                asin=failure.asin,
                                status="excluded",
                                exclusion_reason=failure.reason,
                                natural_position=positions.get(failure.asin),
                            )
                        )
                    item.comparisons = comparisons
                    item.phase = "scoring"
                    item.scored_products = 0
                    self._save(analysis, fingerprint)
                    # 详情页一条都没拿到说明是采集失败，不能记成“无有效样本”让结果看起来像分析完成。
                    if not collected.products:
                        reasons = list(
                            dict.fromkeys(failure.reason for failure in collected.failures)
                        )
                        message = "未取得任何商品详情"
                        if reasons:
                            message += "：" + "；".join(reasons)[:300]
                        raise DomainError(502, "PRODUCT_TERM_COLLECTION_EMPTY", message)
                    product_batches = [
                        collected.products[offset:offset + 10]
                        for offset in range(0, len(collected.products), 10)
                    ]

                    def save_batch(
                        snapshots: list[ProductSnapshot], batch: BatchComparisonSet,
                        comparisons: list[ProductComparison] = comparisons,
                        positions: dict[str, int] = positions,
                        item: TermAnalysisItem = item,
                    ) -> None:
                        for snapshot, draft in zip(snapshots, batch.items, strict=True):
                            if not draft.valid:
                                comparisons.append(
                                    ProductComparison(
                                        asin=snapshot.asin,
                                        status="excluded",
                                        exclusion_reason=draft.exclusion_reason,
                                        natural_position=positions.get(snapshot.asin),
                                    )
                                )
                                continue
                            # 包装数量/计价单位可缺（价格分量会按不可比处理），只有分数必须齐全。
                            assert draft.scores is not None
                            candidate = ProductProfile(
                                asin=snapshot.asin,
                                purpose=draft.purpose,
                                style_structure=draft.style_structure,
                                specifications=draft.specifications,
                                price=snapshot.price if snapshot.price_reliable else None,
                                currency=snapshot.currency if snapshot.price_reliable else None,
                                package_quantity=draft.package_quantity,
                                unit_basis=draft.unit_basis,
                            )
                            comparison = score_product(
                                profile_record.profile, candidate, draft.scores
                            )
                            comparison.natural_position = positions.get(snapshot.asin)
                            comparison.candidate_profile = candidate
                            comparisons.append(comparison)
                        comparisons.sort(
                            key=lambda row: (
                                row.natural_position is None,
                                row.natural_position or SAMPLE_LIMIT + 1,
                            )
                        )
                        item.comparisons = list(comparisons)
                        item.scored_products += len(snapshots)
                        item.updated_at = datetime.now(UTC)
                        self._save(analysis, fingerprint)

                    rate_limited: list[list[ProductSnapshot]] = []
                    fatal_error: DomainError | None = None
                    concurrency = min(_relevance_model_concurrency(), len(product_batches))
                    with ThreadPoolExecutor(
                        max_workers=concurrency,
                        thread_name_prefix="product-relevance",
                    ) as executor:
                        futures: dict[Future[BatchComparisonSet], list[ProductSnapshot]] = {
                            executor.submit(
                                _compare_with_format_fallback,
                                model,
                                profile_record.profile,
                                snapshots,
                            ): snapshots
                            for snapshots in product_batches
                        }
                        for future in as_completed(futures):
                            snapshots = futures[future]
                            try:
                                batch = future.result()
                            except DomainError as error:
                                if (
                                    error.code == "MODEL_PROVIDER_ERROR"
                                    and error.status_code == 503
                                ):
                                    rate_limited.append(snapshots)
                                elif fatal_error is None:
                                    fatal_error = error
                            else:
                                save_batch(snapshots, batch)
                    if fatal_error is not None:
                        raise fatal_error
                    # 429 批次等并发请求全部收束后逐批重试，避免继续触发上游限流。
                    for snapshots in sorted(
                        rate_limited,
                        key=lambda rows: positions.get(rows[0].asin, SAMPLE_LIMIT + 1),
                    ):
                        batch = _compare_with_format_fallback(
                            model, profile_record.profile, snapshots
                        )
                        save_batch(snapshots, batch)
                    item.comparisons = comparisons
                    item.summary = summarize_term(comparisons)
                    item.status = "completed" if item.summary.valid_samples else "no_samples"
                except DomainError as error:
                    item.status = "failed"
                    item.error = error.message[:500]
                    if error.code in PAUSE_CODES:
                        # 访问检查/限流需要人工处理：先落盘已完成部分，恢复后只重采未完成候选。
                        analysis.best_candidate_ids = choose_terms(analysis.items)
                        analysis.complete = False
                        analysis.status = "incomplete"
                        self._annotate(analysis)
                        self._save(analysis, fingerprint)
                        raise
                except Exception as error:
                    item.status = "failed"
                    item.error = (str(error) or type(error).__name__)[:500]
                analysis.best_candidate_ids = choose_terms(analysis.items)
                self._annotate(analysis)
                self._save(analysis, fingerprint)
        winners = choose_terms(analysis.items)
        analysis.best_candidate_ids = winners
        analysis.complete = all(i.status in ("completed", "no_samples") for i in analysis.items)
        if not analysis.complete:
            analysis.status = "incomplete"
        elif winners:
            analysis.status = "completed"
        else:
            # 全部候选都没有可排名结果：分析跑完了，但不能对外宣称“已完成”。
            analysis.status = "awaiting_review"
        if winners:
            with self.repository.connect() as conn:
                existing = conn.execute("SELECT document FROM root_selections WHERE run_id=?", (str(run),)).fetchone()
                if not existing or RootSelection.model_validate_json(existing[0]).source != "manual":
                    selection = RootSelection(project_id=project, run_id=run, candidate_ids=winners,
                                              confirmed_at=datetime.now(UTC), source="automatic", verification_id=analysis.id)
                    conn.execute("INSERT INTO root_selections VALUES(?,?) ON CONFLICT(run_id) DO UPDATE SET document=excluded.document",
                                 (str(run), selection.model_dump_json()))
        self._annotate(analysis)
        self._save(analysis, fingerprint)
        return analysis
