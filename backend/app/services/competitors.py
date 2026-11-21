"""第三步：显式发现、人工确认及不可变版本快照。"""

import hashlib
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Lock
from typing import cast
from uuid import UUID, uuid4

from app.adapters.competitor_csv import parse_competitor_csv
from app.adapters.poe_downloads import (
    PoeArchive,
    PoeBrowserDownloader,
    PoeDownloader,
    PoeDownloadStore,
)
from app.db.repository import Repository
from app.domain.asin_product import ProductSnapshot
from app.domain.competitors import (
    Competitor,
    Competitors,
    CompetitorSource,
    DiscoveryInput,
    PoeDownloadEvidence,
    PoeDownloadInput,
    PoeIntentChoice,
    PriceSettings,
    PriceStatus,
    SelectionInput,
)
from app.domain.core_terms import CoreSelection
from app.domain.errors import DomainError
from app.domain.product_terms import ProductProfile, ProductTermAnalysis
from app.services.amazon_search import AmazonSearch, SearchProvider
from app.services.asin_product import ProductProvider
from app.services.core_terms import CoreService
from app.services.hot_search_core_analysis import HotSearchCoreAnalysis
from app.services.product_profiles import ProductProfileService
from app.services.product_term_scoring import score_product
from app.services.qwen import QwenClient, QwenConfig


SHARED_SNAPSHOT_MAX_AGE = timedelta(hours=24)


def price_status(item: Competitor, settings: PriceSettings) -> PriceStatus:
    if item.price is None or settings.reference_price is None or item.currency != settings.currency:
        return "unknown"
    return (
        "within"
        if abs(item.price - settings.reference_price)
        <= settings.reference_price * settings.tolerance
        else "outside"
    )


class CompetitorService:
    def __init__(
        self,
        repository: Repository,
        core: CoreService,
        products: ProductProvider,
        search: SearchProvider | None = None,
        profiles: ProductProfileService | None = None,
        client: QwenClient | None = None,
        poe_downloader: PoeDownloader | None = None,
    ) -> None:
        self._poe_lock = Lock()
        self._poe_active_runs: set[UUID] = set()
        self.repository = repository
        self.core = core
        self.products = products
        self.search = search or AmazonSearch()
        self.profiles = profiles
        self.client = client
        self.poe_store = PoeDownloadStore(repository.storage_root / "poe_downloads")
        self.poe_downloader = poe_downloader or PoeBrowserDownloader(
            repository.storage_root / "aba", self.poe_store
        )

    def download_poe(self, project: UUID, run: UUID, data: PoeDownloadInput) -> Competitors:
        """下载与解析成功后合并；耗时采集后再次验证版本和核心词快照。"""
        core = self._core(project, run)
        previous = self._latest(run)
        if (previous.version if previous else 0) != data.expected_version:
            raise DomainError(409, "COMPETITOR_VERSION_CONFLICT", "竞对记录已变化，请重新读取")
        if self.repository.get_run(project, run).input.marketplace != "US":
            raise DomainError(422, "POE_MARKETPLACE_UNSUPPORTED", "自动下载目前仅支持美国站")
        allowed = {item.keyword.casefold(): item.keyword for item in core.items}
        keyword = allowed.get(data.keyword.strip().casefold())
        if not keyword:
            raise DomainError(422, "POE_CORE_KEYWORD_REQUIRED", "请选择第二步已确认的核心词")
        if self.profiles is None:
            raise DomainError(409, "PRODUCT_PROFILE_REQUIRED", "请先确认商品四维基准")
        profile = self.profiles.get(project, run)
        if profile.status != "confirmed":
            raise DomainError(409, "PRODUCT_PROFILE_REQUIRED", "请先确认商品四维基准")
        model = self.client or QwenClient(QwenConfig.load(self.repository.storage_root))
        candidates = self.poe_downloader.intents(keyword)
        choice = model.select_poe_intent(profile.profile, keyword, candidates)
        chosen = next((item for item in candidates if item.ref == choice.candidate_ref), None)
        if chosen is None:
            raise DomainError(502, "MODEL_OUTPUT_INVALID", "购买意图不属于页面实际候选")
        artifact = self.poe_downloader.download(run, keyword, chosen.ref)
        if self._core(project, run).id != core.id:
            raise DomainError(409, "COMPETITOR_CONTEXT_CHANGED", "下载期间核心大词变化，请重新读取")
        label = f"商机浏览器原件 {artifact.id} · {keyword}"
        return self.discover(
            project,
            run,
            DiscoveryInput(
                expected_version=data.expected_version,
                source="poe_csv",
                csv_text=artifact.text,
                source_label=label,
            ),
            poe_evidence=PoeDownloadEvidence(
                id=artifact.id,
                filename=artifact.original_filename,
                source_url=artifact.source_url,
                keyword=keyword,
                sha256=artifact.sha256,
                rows=len(artifact.items),
                intent_ref=chosen.ref,
                intent_name=chosen.name,
                intent_reason=choice.reason,
                intent_confidence=choice.confidence,
            ),
            expected_core=core.id,
        )

    def download_poe_all(
        self, project: UUID, run: UUID, expected_version: int,
        retry_keywords: list[str] | None = None,
    ) -> Competitors:
        """同一任务仅允许一次商机采集，防止自动流程与页面重复下载。"""
        with self._poe_lock:
            if run in self._poe_active_runs:
                raise DomainError(
                    409, "COMPETITOR_COLLECTION_RUNNING",
                    "该任务正在采集竞对，请等待当前采集完成后再重试",
                )
            self._poe_active_runs.add(run)
        try:
            return self._download_poe_all(project, run, expected_version, retry_keywords)
        finally:
            with self._poe_lock:
                self._poe_active_runs.discard(run)

    def _download_poe_all(
        self, project: UUID, run: UUID, expected_version: int,
        retry_keywords: list[str] | None = None,
    ) -> Competitors:
        """下载全部核心词的商机商品，合并去重后按四维和价格门槛筛选。"""
        core = self._core(project, run)
        previous = self._latest(run)
        if (previous.version if previous else 0) != expected_version:
            raise DomainError(409, "COMPETITOR_VERSION_CONFLICT", "竞对记录已变化，请重新读取")
        if self.repository.get_run(project, run).input.marketplace != "US":
            raise DomainError(422, "POE_MARKETPLACE_UNSUPPORTED", "自动下载目前仅支持美国站")
        if self.profiles is None:
            raise DomainError(409, "PRODUCT_PROFILE_REQUIRED", "请先确认商品四维基准")
        try:
            profile = self.profiles.get(project, run)
        except DomainError as error:
            if error.code == "PRODUCT_PROFILE_NOT_FOUND":
                raise DomainError(409, "PRODUCT_PROFILE_REQUIRED", "请先确认商品四维基准") from None
            raise
        if profile.status != "confirmed":
            raise DomainError(409, "PRODUCT_PROFILE_REQUIRED", "请先确认商品四维基准")
        allowed = {item.keyword for item in core.items}
        if set(retry_keywords or []) - allowed:
            raise DomainError(422, "POE_CORE_KEYWORD_REQUIRED", "重试词必须属于当前核心词")
        result = (
            previous.model_copy(deep=True)
            if previous and previous.core_selection_id == core.id
            else Competitors(
                id=uuid4(),
                project_id=project,
                run_id=run,
                core_selection_id=core.id,
                core_keywords=[item.keyword for item in core.items],
                version=0,
                created_at=datetime.now(UTC),
            )
        )
        product = self.repository.get_run(project, run).input
        if profile.profile.price is not None:
            result.reference_price = profile.profile.price
            result.currency = profile.profile.currency or result.currency
        elif not previous or previous.core_selection_id != core.id:
            result.reference_price = product.reference_price
            result.currency = product.reference_currency
        result.tolerance = Decimal("0.15")
        result.confirmed_at = None
        model = self.client or QwenClient(QwenConfig.load(self.repository.storage_root))
        completed = {entry.keyword: entry for entry in result.poe_downloads if entry.status == "completed"}
        artifacts: list[tuple[str, PoeArchive]] = []
        result.poe_downloads = []
        for keyword in [item.keyword for item in core.items]:
            stage = "search"
            evidence = PoeDownloadEvidence(id=uuid4(), filename="", keyword=keyword, sha256="", rows=0)
            try:
                prior = completed.get(keyword)
                if prior is not None and keyword not in (retry_keywords or []):
                    stage = "archive"
                    path = self.poe_store.directory / str(run) / f"{prior.id}.csv"
                    raw = path.read_bytes()
                    if hashlib.sha256(raw).hexdigest() != prior.sha256:
                        raise DomainError(422, "POE_ARCHIVE_CHANGED", "已归档商品原件哈希变化，请重试此词")
                    encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
                    text = raw.decode(encoding)
                    items = parse_competitor_csv(text, prior.filename, require_30_day=True)
                    artifact = PoeArchive(prior.id, path, text, prior.sha256, items, prior.filename, prior.source_url)
                    evidence = prior.model_copy(deep=True)
                else:
                    candidates = self.poe_downloader.intents(keyword)
                    stage = "selection"
                    choice: PoeIntentChoice = model.select_poe_intent(profile.profile, keyword, candidates)
                    chosen = next((item for item in candidates if item.ref == choice.candidate_ref), None)
                    if chosen is None:
                        raise DomainError(502, "MODEL_OUTPUT_INVALID", "购买意图不属于页面实际候选")
                    evidence.intent_ref, evidence.intent_name = chosen.ref, chosen.name
                    evidence.intent_reason, evidence.intent_confidence = choice.reason, choice.confidence
                    stage = "download"
                    artifact = self.poe_downloader.download(run, keyword, chosen.ref)
                    evidence.id, evidence.filename = artifact.id, artifact.original_filename
                    evidence.source_url, evidence.sha256 = artifact.source_url, artifact.sha256
                    stage = "parse"
                    items = parse_competitor_csv(artifact.text, artifact.original_filename, require_30_day=True)
                    evidence.rows = len(items)
                evidence.price_qualified_rows = sum(1 for item in items if item.asin != product.asin and item.price_basis == "average_30d" and price_status(item, result) == "within")
                evidence.status, evidence.stage = "completed", "completed"
                artifacts.append((keyword, artifact))
            except DomainError as error:
                evidence.status, evidence.stage = "failed", stage
                evidence.error_code, evidence.error = error.code, error.message
            except (OSError, UnicodeError) as error:
                evidence.status, evidence.stage = "failed", stage
                evidence.error_code, evidence.error = "POE_ARCHIVE_FAILED", str(error)[:300]
            result.poe_downloads.append(evidence)
        if self._core(project, run).id != core.id:
            raise DomainError(409, "COMPETITOR_CONTEXT_CHANGED", "下载期间核心大词变化，请重新读取")
        failures = [entry for entry in result.poe_downloads if entry.status == "failed"]
        if failures:
            result.warnings = [f"{entry.keyword} 在 {entry.stage} 阶段失败：{entry.error_code} · {entry.error}" for entry in failures]
            return self._write(result, expected_version)
        by_asin: dict[str, Competitor] = {}
        for keyword, artifact in artifacts:
            for item in parse_competitor_csv(artifact.text, f"商机探测器 · {keyword}", require_30_day=True):
                if item.asin == product.asin:
                    continue
                for source in item.sources:
                    source.keyword = keyword
                    source.region = "US"
                    source.url = artifact.source_url
                existing = by_asin.get(item.asin)
                if existing:
                    existing.sources.extend(item.sources)
                    existing.price, existing.currency, existing.price_basis = (
                        item.price,
                        item.currency,
                        item.price_basis,
                    )
                else:
                    by_asin[item.asin] = item
        # 多核心词的原始候选合并后可超过人工工作区的 500 条上限。
        # 全量去重后先按真实 30 天均价预筛，再取详情和评分，最终仍最多保留 100 个。
        model.config.validate()
        qualified: list[Competitor] = []
        for item in by_asin.values():
            item.price_status = price_status(item, result)
            if item.price_basis == "average_30d" and item.price_status == "within":
                qualified.append(item)
        cached = self._shared_product_snapshots(run)
        # Search snapshots can contain only a title. Refresh those once through the
        # detail provider before asking the model to extract evidence.
        refreshed = {
            item.asin for item in qualified
            if item.asin in cached
            and not any(part.strip() for part in cached[item.asin].features)
            and not cached[item.asin].description.strip()
        }
        for asin in refreshed:
            cached.pop(asin)
        detail_errors: dict[str, Exception] = {}
        fetch_many = cast(
            Callable[[str, list[str]], list[tuple[str, ProductSnapshot | None, Exception | None]]] | None,
            getattr(self.products, "fetch_many", None),
        )
        missing = [item.asin for item in qualified if item.asin not in cached]
        if callable(fetch_many) and missing:
            for asin, snapshot, error in fetch_many(product.marketplace, missing):
                if asin not in missing:
                    raise DomainError(502, "ASIN_IDENTITY_UNCONFIRMED", "批量详情返回了未请求的 ASIN")
                if snapshot is not None and snapshot.asin == asin and snapshot.title.strip():
                    cached[asin] = snapshot
                    self._cache_product_snapshot(run, snapshot)
                else:
                    detail_errors[asin] = error or DomainError(502, "ASIN_DETAILS_UNAVAILABLE", "批量详情未返回有效商品")
            for asin in missing:
                if asin not in cached and asin not in detail_errors:
                    detail_errors[asin] = DomainError(502, "ASIN_DETAILS_UNAVAILABLE", "批量详情缺少此 ASIN")
        snapshots: list[tuple[Competitor, ProductSnapshot]] = []
        detail_failures: list[str] = []
        for item in qualified:
            try:
                if item.asin in detail_errors:
                    raise detail_errors[item.asin]
                snapshot = cached.get(item.asin)
                fetched = snapshot is None
                if snapshot is None:
                    snapshot = self.products.fetch(product.marketplace, item.asin)
                if snapshot.asin != item.asin or not snapshot.title.strip():
                    raise DomainError(502, "ASIN_IDENTITY_UNCONFIRMED", "商品详情身份不匹配或标题为空")
                if fetched:
                    self._cache_product_snapshot(run, snapshot)
                item.title = snapshot.title
                item.image_url = snapshot.image_url or item.image_url
                snapshots.append((item, snapshot))
            except Exception as error:
                if isinstance(error, DomainError) and error.code == "SEARCH_ACCESS_CHECK":
                    raise
                detail_failures.append(f"{item.asin}：{str(error)[:200]}")
                continue
        kept: list[Competitor] = []
        successful_comparisons = 0
        incomplete_count = 0
        model_failure_count = 0
        excluded_count = 0
        comparison_details: list[str] = []
        for offset in range(0, len(snapshots), 10):
            batch = snapshots[offset : offset + 10]
            comparison_set = model.compare_product_batch(
                profile.profile, [snapshot for _, snapshot in batch]
            )
            for (item, snapshot), draft in zip(batch, comparison_set.items, strict=True):
                if not draft.valid or draft.scores is None:
                    if draft.exclusion_code == "PRODUCT_DATA_INCOMPLETE":
                        incomplete_count += 1
                    elif draft.exclusion_code.startswith("MODEL_"):
                        model_failure_count += 1
                    else:
                        excluded_count += 1
                    comparison_details.append(f"{item.asin}：{draft.exclusion_reason or '未返回有效比较分数'}")
                    continue
                successful_comparisons += 1
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
                comparison = score_product(profile.profile, candidate, draft.scores)
                item.relevance_score = comparison.overall_score
                if (
                    comparison.overall_score is not None
                    and comparison.overall_score > Decimal("40")
                    and item.price_status == "within"
                ):
                    item.decision = "include"
                    item.reason = (
                        f"方法三自动筛选：四维相关度 {comparison.overall_score} 分，"
                        "且 30 天平均售价在本品 ±15% 范围内。"
                    )
                    kept.append(item)
        ranked = sorted(
            kept,
            key=lambda item: (-(item.relevance_score or Decimal(0)), item.asin),
        )
        result.items = ranked[:100]
        result.warnings = [
            f"已合并 {len(artifacts)} 个核心大词的商机探测器下载，去重后 {len(by_asin)} 个原始候选、价格合格 {len(qualified)} 个；先按 30 天平均售价 ±15% 预筛，再按四维相关度 >40 筛选，符合条件的商品已自动确认为竞对。"
        ]
        result.warnings.append(
            f"商品比较结果：成功比较 {successful_comparisons} 个，资料不足 {incomplete_count} 个，"
            f"模型处理失败 {model_failure_count} 个，其他排除 {excluded_count} 个，"
            f"详情采集失败 {len(detail_failures)} 个；符合筛选条件 {len(ranked)} 个。"
        )
        if refreshed:
            result.warnings.append(f"已对 {len(refreshed)} 个仅有标题的缓存商品补抓详情。")
        if detail_failures:
            result.warnings.append(
                f"有 {len(detail_failures)} 个价格合格 ASIN 的商品详情采集失败；失败商品未进入相关度分析。"
            )
        result.warnings.extend(f"详情采集失败 · {reason}" for reason in detail_failures)
        result.warnings.extend(comparison_details)
        if len(ranked) > 100:
            result.warnings.append(
                f"共有 {len(ranked)} 个商品满足条件；受反向拓词单次最多 100 个 ASIN 限制，"
                "已按相关度保留前 100 个。"
            )
        result.confirmed_at = datetime.now(UTC) if result.items else None
        return self._write(result, expected_version)

    def poe_file(self, project: UUID, run: UUID, artifact_id: UUID) -> Path:
        workspace = self.get(project, run)
        if not any(item.id == artifact_id and item.status == "completed" for item in workspace.poe_downloads):
            raise DomainError(404, "POE_DOWNLOAD_NOT_FOUND", "没有找到此任务的下载原件")
        path = self.poe_store.directory / str(run) / f"{artifact_id}.csv"
        if not path.is_file():
            raise DomainError(404, "POE_DOWNLOAD_NOT_FOUND", "本地下载原件已不存在")
        return path

    def _core(self, project: UUID, run: UUID) -> CoreSelection:
        self.repository.get_run(project, run)
        try:
            result = self.core.selection(project, run)
        except DomainError as error:
            if error.status_code != 404:
                raise
            raise DomainError(
                409, "CORE_SELECTION_REQUIRED", "请先保存第二步核心大词选择"
            ) from None
        if result.stale:
            raise DomainError(409, "CORE_SELECTION_STALE", "核心大词已过期，请先重新确认第二步")
        return result

    def _latest(self, run: UUID) -> Competitors | None:
        with self.repository.connect() as conn:
            row = conn.execute(
                "SELECT document FROM competitor_versions WHERE run_id=? ORDER BY version DESC LIMIT 1",
                (str(run),),
            ).fetchone()
        return Competitors.model_validate_json(row[0]) if row else None

    def _four_dimension_scores(self, run: UUID) -> dict[str, Decimal]:
        """复用最近一次完整四维分析；同一 ASIN 的多条有效证据取平均分。"""
        with self.repository.connect() as conn:
            row = conn.execute(
                "SELECT document FROM product_term_analyses WHERE run_id=? "
                "ORDER BY seq DESC LIMIT 1",
                (str(run),),
            ).fetchone()
        if not row:
            return {}
        analysis = ProductTermAnalysis.model_validate_json(row[0])
        if analysis.status != "completed" or not analysis.complete:
            return {}
        evidence: dict[str, list[Decimal]] = {}
        for term in analysis.items:
            for comparison in term.comparisons:
                if comparison.status == "excluded" or comparison.overall_score is None:
                    continue
                evidence.setdefault(comparison.asin, []).append(comparison.overall_score)
        return {asin: sum(values, Decimal(0)) / len(values) for asin, values in evidence.items()}

    def _shared_product_snapshots(
        self, run: UUID, *, now: datetime | None = None
    ) -> dict[str, ProductSnapshot]:
        """读取同一任务前两步 24 小时内的详情证据，ASIN 冲突时保留最新快照。"""
        cutoff = (now or datetime.now(UTC)) - SHARED_SNAPSHOT_MAX_AGE
        with self.repository.connect() as conn:
            step_one = conn.execute(
                "SELECT document FROM product_term_analyses WHERE run_id=? ORDER BY seq DESC",
                (str(run),),
            ).fetchall()
            step_two = conn.execute(
                "SELECT document FROM hot_search_core_analyses WHERE run_id=? ORDER BY seq DESC",
                (str(run),),
            ).fetchall()
        shared: dict[str, ProductSnapshot] = {}

        def keep(snapshot: ProductSnapshot) -> None:
            current = shared.get(snapshot.asin)
            if snapshot.captured_at < cutoff:
                return
            if current is None or snapshot.captured_at > current.captured_at:
                shared[snapshot.asin] = snapshot

        for (document,) in step_one:
            try:
                analysis = ProductTermAnalysis.model_validate_json(document)
            except ValueError:
                continue
            for item in analysis.items:
                if item.collection is not None:
                    for snapshot in item.collection.products:
                        keep(snapshot)
        for (document,) in step_two:
            try:
                analysis = HotSearchCoreAnalysis.model_validate_json(document)
            except ValueError:
                continue
            for snapshot in analysis.snapshots.values():
                keep(snapshot)
        cache = self.repository.storage_root / "competitor_product_snapshots" / str(run)
        if cache.is_dir():
            for path in cache.glob("*.json"):
                try:
                    keep(ProductSnapshot.model_validate_json(path.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    continue
        return shared

    def _cache_product_snapshot(self, run: UUID, snapshot: ProductSnapshot) -> None:
        """持久化第三步新采集详情；缓存失败不应让已成功的页面采集失效。"""
        if not re.fullmatch(r"[A-Z0-9]{10}", snapshot.asin):
            return
        directory = self.repository.storage_root / "competitor_product_snapshots" / str(run)
        temporary: Path | None = None
        try:
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / f"{snapshot.asin}.json"
            temporary = directory / f".{snapshot.asin}.{uuid4().hex}.tmp"
            temporary.write_text(snapshot.model_dump_json(), encoding="utf-8")
            temporary.replace(target)
        except OSError:
            try:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _score_snapshots(
        reference: ProductProfile,
        snapshots: list[ProductSnapshot],
        model: QwenClient,
    ) -> dict[str, Decimal]:
        scores: dict[str, Decimal] = {}
        for offset in range(0, len(snapshots), 10):
            batch = snapshots[offset : offset + 10]
            comparison_set = model.compare_product_batch(reference, batch)
            for snapshot, draft in zip(batch, comparison_set.items, strict=True):
                if not draft.valid or draft.scores is None:
                    continue
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
                comparison = score_product(reference, candidate, draft.scores)
                if comparison.overall_score is not None:
                    scores[snapshot.asin] = comparison.overall_score
        return scores

    def _enrich_front_candidates(
        self,
        project: UUID,
        run: UUID,
        marketplace: str,
        items: list[Competitor],
        warnings: list[str],
    ) -> None:
        if self.profiles is None:
            return
        try:
            profile = self.profiles.get(project, run)
        except DomainError as error:
            if error.code == "PRODUCT_PROFILE_NOT_FOUND":
                warnings.append("尚无已确认四维基准，方法二仅保留搜索证据。")
                return
            raise
        if profile.status != "confirmed":
            warnings.append("四维基准尚未确认，方法二仅保留搜索证据。")
            return
        known_scores = self._four_dimension_scores(run)
        cached = self._shared_product_snapshots(run)
        snapshots: dict[str, ProductSnapshot] = {}
        failures = 0
        for item in items:
            try:
                snapshot = cached.get(item.asin)
                fetched = snapshot is None
                if snapshot is None:
                    snapshot = self.products.fetch(marketplace, item.asin)
                if snapshot.asin != item.asin or not snapshot.title.strip():
                    failures += 1
                    continue
                if fetched:
                    self._cache_product_snapshot(run, snapshot)
                snapshots[item.asin] = snapshot
                item.title = snapshot.title
                item.image_url = snapshot.image_url or item.image_url
                item.price = snapshot.price
                item.currency = snapshot.currency
                item.price_basis = "live" if snapshot.price else "unknown"
            except Exception:
                failures += 1
        missing_scores = [
            snapshot for asin, snapshot in snapshots.items() if asin not in known_scores
        ]
        if missing_scores:
            model = self.client or QwenClient(QwenConfig.load(self.repository.storage_root))
            model.config.validate()
            known_scores.update(self._score_snapshots(profile.profile, missing_scores, model))
        for item in items:
            item.relevance_score = known_scores.get(item.asin)
        if failures:
            warnings.append(f"方法二有 {failures} 个 ASIN 的商品详情采集失败，已保留搜索证据。")

    def get(self, project: UUID, run: UUID) -> Competitors:
        self.repository.get_run(project, run)
        result = self._latest(run)
        if not result:
            raise DomainError(404, "COMPETITORS_NOT_FOUND", "尚未添加竞对候选")
        try:
            result.stale = self._core(project, run).id != result.core_selection_id
        except DomainError as error:
            if error.code not in ("CORE_SELECTION_REQUIRED", "CORE_SELECTION_STALE"):
                raise
            result.stale = True
        return result

    def _write(self, result: Competitors, expected: int) -> Competitors:
        with self.repository.connect() as conn:
            row = conn.execute(
                "SELECT MAX(version) FROM competitor_versions WHERE run_id=?", (str(result.run_id),)
            ).fetchone()
            assert row is not None
            version = int(cast(int | None, row[0]) or 0)
            if version != expected:
                raise DomainError(
                    409, "COMPETITOR_VERSION_CONFLICT", "竞对记录已变化，请重新读取后操作"
                )
            if self._core(result.project_id, result.run_id).id != result.core_selection_id:
                raise DomainError(
                    409, "COMPETITOR_CONTEXT_CHANGED", "采集期间核心大词变化，请重新读取"
                )
            result.version = version + 1
            result.id = uuid4()
            result.created_at = datetime.now(UTC)
            conn.execute(
                "INSERT INTO competitor_versions (id, run_id, version, document) VALUES (?,?,?,?)",
                (str(result.id), str(result.run_id), result.version, result.model_dump_json()),
            )
        return result

    def discover(
        self,
        project: UUID,
        run: UUID,
        data: DiscoveryInput,
        *,
        poe_evidence: PoeDownloadEvidence | None = None,
        expected_core: UUID | None = None,
    ) -> Competitors:
        core = self._core(project, run)
        if expected_core is not None and core.id != expected_core:
            raise DomainError(409, "COMPETITOR_CONTEXT_CHANGED", "核心大词已变化，请重新读取")
        previous = self._latest(run)
        if (previous.version if previous else 0) != data.expected_version:
            raise DomainError(409, "COMPETITOR_VERSION_CONFLICT", "竞对记录已变化，请重新读取")
        result = (
            previous.model_copy(deep=True)
            if previous and previous.core_selection_id == core.id
            else Competitors(
                id=uuid4(),
                project_id=project,
                run_id=run,
                core_selection_id=core.id,
                core_keywords=[item.keyword for item in core.items],
                version=0,
                created_at=datetime.now(UTC),
            )
        )
        product = self.repository.get_run(project, run).input
        # 新候选集优先继承第一步确认的四维基准；已有工作区保留人工核对价格。
        if not previous or previous.core_selection_id != core.id:
            result.reference_price = product.reference_price
            result.currency = product.reference_currency
            if self.profiles is not None:
                try:
                    profile = self.profiles.get(project, run)
                except DomainError as error:
                    if error.code != "PRODUCT_PROFILE_NOT_FOUND":
                        raise
                else:
                    if profile.status == "confirmed" and profile.profile.price is not None:
                        result.reference_price = profile.profile.price
                        result.currency = profile.profile.currency or result.currency
        incoming: list[Competitor] = []
        warnings: list[str] = []
        if data.source == "manual":
            incoming = [
                Competitor(
                    asin=asin,
                    sources=[
                        CompetitorSource(
                            kind="manual",
                            label=data.source_label.strip() or "市场调研人工输入",
                        )
                    ],
                )
                for asin in dict.fromkeys(data.asins)
            ]
        elif data.source == "poe_csv":
            incoming = parse_competitor_csv(data.csv_text, data.source_label.strip())
            if poe_evidence:
                for item in incoming:
                    for source in item.sources:
                        source.keyword = poe_evidence.keyword
                        source.url = poe_evidence.source_url
                        source.region = "US"
            warnings.append(
                "商机探测器商品 CSV 为导出快照；平均售价与当前售价口径不同。缺失币种需核对。"
            )
        elif data.source == "front_search":
            incoming, warnings = self._front(product.marketplace, run, core, data)
            self._enrich_front_candidates(
                project, run, product.marketplace, incoming, warnings
            )
        else:
            by_asin = {item.asin: item for item in result.items}
            if any(asin not in by_asin for asin in data.asins):
                raise DomainError(422, "COMPETITOR_UNKNOWN_ASIN", "只能读取当前候选的商品详情")
            for asin in dict.fromkeys(data.asins):
                try:
                    detail = self.products.fetch(product.marketplace, asin)
                    if detail.asin != asin or not detail.title.strip():
                        raise DomainError(502, "ASIN_IDENTITY_UNCONFIRMED", "商品身份未确认")
                    self._cache_product_snapshot(run, detail)
                    incoming.append(
                        Competitor(
                            asin=asin,
                            title=detail.title,
                            image_url=detail.image_url,
                            price=detail.price,
                            currency=detail.currency,
                            price_basis="live" if detail.price else "unknown",
                            sources=[
                                CompetitorSource(
                                    kind="details",
                                    label="商品页详情",
                                    url=detail.source_url,
                                    captured_at=detail.captured_at,
                                )
                            ],
                        )
                    )
                except DomainError as error:
                    warnings.append(f"{asin}：{error.message}")
        by_asin = {item.asin: item for item in result.items}
        for item in incoming:
            if not re.fullmatch(r"[A-Z0-9]{10}", item.asin):
                raise DomainError(502, "COMPETITOR_INVALID_ASIN", "供应商返回了无效 ASIN")
            if item.asin == product.asin:
                warnings.append(f"已排除本品 {item.asin}")
                continue
            existing = by_asin.get(item.asin)
            if existing:
                for source in item.sources:
                    # 重试同一来源不重复计数；每次快照版本仍记录采集时间。
                    existing.sources = [
                        s
                        for s in existing.sources
                        if (s.kind, s.keyword, s.label)
                        != (source.kind, source.keyword, source.label)
                    ]
                    existing.sources.append(source)
                if item.title:
                    existing.title = item.title
                if item.image_url:
                    existing.image_url = item.image_url
                if data.source == "front_search":
                    existing.relevance_score = item.relevance_score
                    if item.price is not None:
                        existing.price, existing.currency = item.price, item.currency
                        existing.price_basis = item.price_basis
                    existing.decision, existing.reason, existing.price_override_reason = (
                        "pending",
                        "",
                        "",
                    )
                elif data.source in ("details", "poe_csv"):
                    existing.price, existing.currency = item.price, item.currency
                    existing.price_basis = item.price_basis
                    existing.decision, existing.reason, existing.price_override_reason = (
                        "pending",
                        "",
                        "",
                    )
            else:
                by_asin[item.asin] = item
        if len(by_asin) > 500:
            raise DomainError(422, "COMPETITOR_LIMIT", "候选最多 500 个，请减少本次输入")
        result.items = list(by_asin.values())
        relevance_scores = self._four_dimension_scores(run) if data.source == "front_search" else {}
        for item in result.items:
            item.keyword_hits = len(
                {s.keyword.casefold() for s in item.sources if s.kind == "front_search"}
            )
            item.price_status = price_status(item, result)
            if data.source == "front_search" and item.relevance_score is None:
                item.relevance_score = relevance_scores.get(item.asin)
        if data.source == "front_search":
            result.items.sort(
                key=lambda item: (
                    -item.keyword_hits,
                    item.relevance_score is None,
                    -(item.relevance_score or Decimal(0)),
                    item.asin,
                )
            )
        result.warnings = list(dict.fromkeys(result.warnings + warnings))
        result.confirmed_at = None
        if poe_evidence:
            result.poe_downloads.append(poe_evidence)
        return self._write(result, data.expected_version)

    def _front(
        self,
        marketplace: str,
        run: UUID,
        core: CoreSelection,
        data: DiscoveryInput,
    ) -> tuple[list[Competitor], list[str]]:
        allowed = {item.keyword.casefold(): item.keyword for item in core.items}
        requested = list(dict.fromkeys(term.strip().casefold() for term in data.keywords))
        if requested and set(requested) != set(allowed):
            raise DomainError(
                422,
                "COMPETITOR_KEYWORD_INVALID",
                "前台搜索必须使用当前已保存的全部核心大词",
            )
        terms = list(allowed.values())
        found: dict[str, Competitor] = {}
        warnings = [
            "搜索范围：全部核心大词、各第一页前 30 个自然结果；先按重复次数、再按已有四维相关度从高到低排序，并非全站排名。"
        ]
        successes = 0
        evidence = self.repository.storage_root / "competitor_evidence" / str(run) / str(uuid4())
        try:
            with self.search.open(marketplace, evidence) as session:
                for index, term in enumerate(terms):
                    try:
                        page = session.search(term, 1, f"term-{index}")
                        successes += 1
                        seen: set[str] = set()
                        for card in page.cards[:30]:
                            if card.asin in seen:
                                continue
                            seen.add(card.asin)
                            candidate = found.setdefault(
                                card.asin, Competitor(asin=card.asin, image_url=card.image_url)
                            )
                            candidate.sources.append(
                                CompetitorSource(
                                    kind="front_search",
                                    label="前台自然搜索",
                                    keyword=term,
                                    position=card.position,
                                    url=page.url,
                                    region=page.region,
                                )
                            )
                    except DomainError as error:
                        warnings.append(f"{term}：{error.message}")
        except DomainError as error:
            warnings.append(error.message)
        if successes < len(terms):
            warnings.append(f"采集不完整：成功 {successes}/{len(terms)} 个词；失败不代表没有竞对。")
        if len(terms) == 1:
            warnings.append("当前仅搜索 1 个词，候选是单词自然结果，尚无跨词重复证据。")
        minimum = 1 if len(terms) == 1 else 2
        ordered = sorted(found.values(), key=lambda item: (-len(item.sources), item.asin))
        return [item for item in ordered if len(item.sources) >= minimum], warnings

    def select(self, project: UUID, run: UUID, data: SelectionInput) -> Competitors:
        core = self._core(project, run)
        result = self.get(project, run)
        if result.stale or result.core_selection_id != core.id:
            raise DomainError(409, "COMPETITORS_STALE", "候选已过期，请按当前核心大词重新添加候选")
        reviews = {item.asin: item for item in data.reviews}
        if len(reviews) != len(data.reviews) or set(reviews) != {
            item.asin for item in result.items
        }:
            raise DomainError(
                422, "COMPETITOR_REVIEWS_INVALID", "请逐项提交全部候选，ASIN 不得重复或越界"
            )
        if not 1 <= sum(item.decision == "include" for item in data.reviews) <= 100:
            raise DomainError(422, "COMPETITOR_SELECTION_INVALID", "请人工保留 1 至 100 个竞对")
        result.reference_price, result.currency, result.tolerance = (
            data.reference_price,
            data.currency,
            data.tolerance,
        )
        for item in result.items:
            review = reviews[item.asin]
            item.price_status = price_status(item, result)
            if review.decision != "pending" and not review.reason:
                raise DomainError(422, "COMPETITOR_REASON_REQUIRED", "保留或排除须填写人工核对理由")
            if (
                review.decision == "include"
                and item.price_status != "within"
                and not review.price_override_reason
            ):
                raise DomainError(
                    422,
                    "COMPETITOR_PRICE_REVIEW_REQUIRED",
                    "价格未知或超出价格段，保留须填写价格核对说明",
                )
            item.decision, item.reason, item.price_override_reason = (
                review.decision,
                review.reason,
                review.price_override_reason,
            )
        previous = self.get(project, run)
        if (
            previous.confirmed_at
            and previous.model_dump(exclude={"confirmed_at"})
            == result.model_dump(exclude={"confirmed_at"})
            and data.expected_version in (previous.version, previous.version - 1)
        ):
            # 丢失响应后的同内容重试可读取成功结果；不得覆盖不同的最新内容。
            return previous
        result.confirmed_at = datetime.now(UTC)
        return self._write(result, data.expected_version)
