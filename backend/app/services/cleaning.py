"""四路文本汇合、可追溯快照及带版本的人工审核。"""

import csv
import hashlib
import io
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal, cast
from uuid import UUID, uuid4

from app.db.repository import Repository
from app.domain import taxonomy
from app.domain.aba import AbaQueryReport
from app.domain.aba_expansion import ExpansionReport
from app.domain.cleaning import (
    ALGORITHM,
    DICTIONARY,
    Category,
    CleaningPage,
    CleaningReport,
    CleaningRequest,
    Evidence,
    Frequency,
    Phrase,
    Review,
    RootReview,
    Source,
    analyze,
)
from app.domain.errors import DomainError
from app.domain.product_understanding import (
    ProductConfirmation,
    ProductUnderstanding,
    UnderstandingResult,
)
from app.domain.text import phrase_key
from app.domain.third_party import ImportReport
from app.services.aba_expansion import ExpansionService
from app.services.third_party import ThirdPartyService

if TYPE_CHECKING:
    from app.domain.decisions import CleaningDecision
    from app.services.qwen import QwenClient

CLASSIFICATION_PROTOCOL = "cleaning-classification-v3"
CLASSIFICATION_BATCH_SIZE = 20
# 模型请求失败可自动重试；密钥/配置错误与"付费结果未知"不重试，只告警并保留进度。
CLASSIFICATION_ATTEMPTS = 3
RETRYABLE_MODEL_ERRORS = {"MODEL_OUTPUT_INVALID", "MODEL_PROVIDER_ERROR"}
FATAL_MODEL_ERRORS = {"MODEL_NOT_CONFIGURED", "MODEL_CONFIG_INVALID"}
log = logging.getLogger(__name__)
CLASSIFICATION_CONCURRENCY = 3


def _review_cleaning_batches(
    model: "QwenClient", product: ProductUnderstanding, roots: list[Frequency]
) -> list["CleaningDecision"]:
    """并发审核小批词根；executor.map 保持输入批次的输出顺序。"""
    batches = [
        roots[offset : offset + CLASSIFICATION_BATCH_SIZE]
        for offset in range(0, len(roots), CLASSIFICATION_BATCH_SIZE)
    ]
    if not batches:
        return []
    with ThreadPoolExecutor(
        max_workers=min(CLASSIFICATION_CONCURRENCY, len(batches)),
        thread_name_prefix="cleaning-classification",
    ) as executor:
        return list(
            executor.map(lambda batch: model.review_cleaning(product, batch, []), batches)
        )


class CleaningService:
    def __init__(
        self,
        repository: Repository,
        expansion: ExpansionService,
        third_party: ThirdPartyService,
        model_client: "QwenClient | None" = None,
    ) -> None:
        self.repository, self.expansion, self.third_party = repository, expansion, third_party
        self.model_client = model_client

    def _product(self, run: UUID) -> ProductUnderstanding:
        with self.repository.connect() as conn:
            confirmed = conn.execute(
                "SELECT document FROM product_confirmations WHERE run_id=? ORDER BY version DESC LIMIT 1",
                (str(run),),
            ).fetchone()
            understood = conn.execute(
                "SELECT document FROM product_understandings WHERE run_id=? AND status='succeeded'",
                (str(run),),
            ).fetchone()
        if confirmed:
            return ProductConfirmation.model_validate_json(confirmed[0]).data.product
        if understood:
            return UnderstandingResult.model_validate_json(understood[0]).product
        raise DomainError(409, "CLEANING_PRODUCT_MISSING", "缺少已识别的商品属性，无法自动分类")

    def sources(self, project: UUID, run: UUID, request: CleaningRequest) -> list[Source]:
        self.repository.get_run(project, run)
        with self.repository.connect() as conn:
            aba_keys = conn.execute(
                "SELECT id FROM aba_expansions WHERE run_id=? ORDER BY created_at DESC, seq DESC",
                (str(run),),
            ).fetchall()
            seller_keys = conn.execute(
                "SELECT id FROM third_party_imports WHERE run_id=?", (str(run),)
            ).fetchall()
        abas: list[ExpansionReport] = [
            self.expansion.get(project, run, UUID(r[0])) for r in aba_keys
        ]
        sellers: list[ImportReport] = [
            self.third_party.get(project, run, UUID(r[0])) for r in seller_keys
        ]
        sellers.sort(key=lambda r: (r.confirmed_at or r.created_at, str(r.id)), reverse=True)
        result: list[Source] = []
        selected_seller_ids: set[UUID] = set()
        # 覆盖检查按方向和查询逐项执行：ABA 需匹配报告周，三方词表需匹配自身周期。
        # 允许显式截断的三方导出参与文本清洗，但把 truncated 留在来源证据中。
        for direction in ("forward", "reverse"):
            _, _, queries = self.expansion.context(project, run, direction)
            aba = next(
                (
                    r
                    for r in abas
                    if not r.stale
                    and r.direction == direction
                    and r.week_end == request.aba_week_end
                ),
                None,
            )
            if not aba or aba.status != "succeeded":
                raise DomainError(
                    409, "CLEANING_SOURCES_INCOMPLETE", "所选周的 ABA 正反向必须均采集成功且未过期"
                )
            result.append(
                Source(
                    id=aba.id,
                    provider="aba",
                    direction=direction,
                    query="全部查询",
                    period_start=aba.week_start,
                    period_end=aba.week_end,
                )
            )
            for query in queries:
                seller = next(
                    (
                        r
                        for r in sellers
                        if not r.stale
                        and r.status == "confirmed"
                        and r.metadata.direction == direction
                        and any(
                            phrase_key(source_query) == phrase_key(query)
                            for source_query in r.metadata.source_queries
                        )
                        and r.metadata.period_kind == request.seller_period
                        and r.metadata.period_start == request.seller_start
                        and r.metadata.period_end == request.seller_end
                    ),
                    None,
                )
                if not seller or not (seller.complete or seller.truncated):
                    raise DomainError(
                        409,
                        "CLEANING_SOURCES_INCOMPLETE",
                        "所选周期的卖家精灵词表须覆盖每个核心词及竞品 ASIN",
                    )
                if seller.id in selected_seller_ids:
                    continue
                selected_seller_ids.add(seller.id)
                result.append(
                    Source(
                        id=seller.id,
                        provider="sellersprite",
                        direction=direction,
                        query=seller.metadata.query,
                        period_start=seller.metadata.period_start,
                        period_end=seller.metadata.period_end,
                        period_kind=seller.metadata.period_kind,
                        collected_at=seller.metadata.collected_at,
                        truncated=seller.truncated,
                    )
                )
        return result

    def create(self, project: UUID, run: UUID, request: CleaningRequest) -> CleaningReport:
        sources = self.sources(project, run, request)
        fingerprint = hashlib.sha256(
            (
                request.model_dump_json()
                + ALGORITHM
                + DICTIONARY
                + "".join(str(s.id) for s in sources)
            ).encode()
        ).hexdigest()
        with self.repository.connect() as conn:
            old = conn.execute(
                "SELECT id FROM cleaning_analyses WHERE run_id=? AND fingerprint=?",
                (str(run), fingerprint),
            ).fetchone()
        if old:
            return self.get(project, run, UUID(old[0]))
        phrases: list[Phrase] = []

        def add(evidence: Evidence) -> None:
            # 第六步汇总只逐行提取关键词；重复来源行必须保留，指标仍留在来源证据中。
            phrases.append(Phrase(keyword=phrase_key(evidence.original), evidence=[evidence]))

        for source in sources:
            if source.provider == "aba":
                self.expansion.get(project, run, source.id)
                with self.repository.connect() as conn:
                    stored_row = conn.execute(
                        "SELECT results FROM aba_expansions WHERE id=? AND run_id=?",
                        (str(source.id), str(run)),
                    ).fetchone()
                assert stored_row is not None
                stored = cast(str, stored_row[0])
                documents: list[dict[str, object]] = json.loads(stored)
                for document in documents:
                    query = AbaQueryReport.model_validate(document)
                    for index, evidence in enumerate(query.rows):
                        add(
                            Evidence(
                                source_id=source.id,
                                query=query.query,
                                original=evidence.keyword,
                                source_row=index + 1,
                                payload=json.dumps(
                                    {
                                        "parsed": evidence.model_dump(mode="json"),
                                        "raw": query.raw_rows[index]
                                        if index < len(query.raw_rows)
                                        else None,
                                    },
                                    ensure_ascii=False,
                                ),
                            )
                        )
            else:
                imported = self.third_party.get(project, run, source.id)
                for row in self.third_party.page(
                    project, run, source.id, 0, imported.imported_rows
                ).items:
                    add(
                        Evidence(
                            source_id=source.id,
                            query=source.query,
                            original=row.keyword,
                            source_row=row.row_number,
                            payload=row.model_dump_json(),
                        )
                    )
        phrases.sort(
            key=lambda p: (p.keyword, str(p.evidence[0].source_id), p.evidence[0].source_row)
        )
        frequencies, tokens = analyze([phrase.keyword for phrase in phrases])
        report = CleaningReport(
            id=uuid4(),
            project_id=project,
            run_id=run,
            created_at=datetime.now(UTC),
            request=request,
            sources=sources,
            phrase_count=len(phrases),
            token_count=tokens,
            frequency_count=len(frequencies),
        )
        with self.repository.connect() as conn:
            conn.execute(
                "INSERT INTO cleaning_analyses (id, run_id, fingerprint, document, phrases, frequencies) VALUES(?,?,?,?,?,?) ON CONFLICT (run_id, fingerprint) DO NOTHING",
                (
                    str(report.id),
                    str(run),
                    fingerprint,
                    report.model_dump_json(),
                    json.dumps([p.model_dump(mode="json") for p in phrases]),
                    json.dumps([f.model_dump() for f in frequencies]),
                ),
            )
            key_row = conn.execute(
                "SELECT id FROM cleaning_analyses WHERE run_id=? AND fingerprint=?",
                (str(run), fingerprint),
            ).fetchone()
        assert key_row is not None
        key = cast(str, key_row[0])
        return self.get(project, run, UUID(key))

    def get(self, project: UUID, run: UUID, key: UUID) -> CleaningReport:
        self.repository.get_run(project, run)
        with self.repository.connect() as conn:
            row = conn.execute(
                "SELECT document FROM cleaning_analyses WHERE id=? AND run_id=?",
                (str(key), str(run)),
            ).fetchone()
            reviewed = conn.execute(
                "SELECT version,document FROM cleaning_reviews WHERE analysis_id=? ORDER BY version DESC LIMIT 1",
                (str(key),),
            ).fetchone()
        if not row:
            raise DomainError(404, "CLEANING_NOT_FOUND", "词频分析不存在")
        report = CleaningReport.model_validate_json(row[0])
        if reviewed:
            report.version, report.review = reviewed[0], Review.model_validate_json(reviewed[1])
        try:
            report.stale = self.sources(project, run, report.request) != report.sources
        except DomainError as error:
            if error.status_code != 409:
                raise
            report.stale = True
        return report

    def classification_progress(self, project: UUID, run: UUID, key: UUID) -> dict[str, object]:
        """只读取持久化的批次结果，检查快照归属后才返回进度。"""
        report = self.get(project, run, key)
        with self.repository.connect() as conn:
            saved = conn.execute(
                "SELECT status, document FROM cleaning_classifications WHERE analysis_id=?", (str(key),)
            ).fetchone()
        total = report.frequency_count
        if not saved:
            return {"status": "not_started", "completed": 0, "total": total, "next_retry_at": None}
        status = str(saved[0])
        if status not in {"running", "failed", "outcome_pending", "succeeded"}:
            status = "outcome_pending"
        document: dict[str, object] = {}
        try:
            parsed = json.loads(saved[1])
            if isinstance(parsed, dict):
                document = parsed
        except (TypeError, ValueError):
            document = {}
        completed = 0
        # 进度按"词根数"算，不按标签数算：一个词根多个标签只计一次。
        if report.review is not None:
            completed = len({item.term for item in report.review.roots})
        else:
            roots = document.get("roots", [])
            if isinstance(roots, list):
                completed = len({str(item.get("term")) for item in roots if isinstance(item, dict)})
        return {
            "status": status,
            "completed": min(completed, total),
            "total": total,
            "next_retry_at": document.get("next_retry_at"),
        }

    def history(self, project: UUID, run: UUID) -> list[CleaningReport]:
        self.repository.get_run(project, run)
        with self.repository.connect() as conn:
            rows = conn.execute(
                "SELECT id FROM cleaning_analyses WHERE run_id=? ORDER BY seq DESC LIMIT 50",
                (str(run),),
            ).fetchall()
        return [self.get(project, run, UUID(r[0])) for r in rows]

    def page(
        self,
        project: UUID,
        run: UUID,
        key: UUID,
        kind: Literal["phrases", "frequencies"],
        offset: int,
        limit: int,
    ) -> CleaningPage:
        self.get(project, run, key)
        with self.repository.connect() as conn:
            # 列名来自枚举值，不来自自由文本。
            stored_row = conn.execute(
                f"SELECT {kind} FROM cleaning_analyses WHERE id=?", (str(key),)
            ).fetchone()
        assert stored_row is not None
        stored = cast(str, stored_row[0])
        rows = json.loads(stored)
        page = CleaningPage(total=len(rows), offset=offset, limit=limit)
        if kind == "phrases":
            page.phrases = [Phrase.model_validate(r) for r in rows[offset : offset + limit]]
        else:
            page.frequencies = [Frequency.model_validate(r) for r in rows[offset : offset + limit]]
        return page

    def review(self, project: UUID, run: UUID, key: UUID, review: Review) -> CleaningReport:
        report = self.get(project, run, key)
        if report.stale:
            raise DomainError(409, "CLEANING_STALE", "上游已变更，请重新生成分析")
        terms = {
            r.term
            for r in self.page(
                project, run, key, "frequencies", 0, report.frequency_count
            ).frequencies
        }
        phrases = {
            r.keyword
            for r in self.page(project, run, key, "phrases", 0, report.phrase_count).phrases
        }
        if (
            not review.reason.strip()
            or not review.roots
            or any(r.term not in terms for r in review.roots)
            or any(e.keyword not in phrases or not e.reason.strip() for e in review.exclusions)
        ):
            raise DomainError(
                422, "CLEANING_REVIEW_INVALID", "请选择至少一个实际候选词根，填写审核及排除原因"
            )
        if len({(r.term, r.category) for r in review.roots}) != len(review.roots) or len(
            {e.keyword for e in review.exclusions}
        ) != len(review.exclusions):
            raise DomainError(422, "CLEANING_REVIEW_DUPLICATE", "审核条目不可重复")
        with self.repository.connect() as conn:
            # 写锁内再次检查来源，防止候选读取后上游并发提交导致过期审核落库。
            if self.get(project, run, key).stale:
                raise DomainError(409, "CLEANING_STALE", "上游已变更，请重新生成分析")
            version_row = conn.execute(
                "SELECT COALESCE(MAX(version),0) FROM cleaning_reviews WHERE analysis_id=?",
                (str(key),),
            ).fetchone()
            assert version_row is not None
            version = cast(int, version_row[0])
            if version != review.expected_version:
                raise DomainError(409, "CLEANING_VERSION_CONFLICT", "审核已更新，请刷新后再提交")
            conn.execute(
                "INSERT INTO cleaning_reviews VALUES(?,?,?)",
                (str(key), version + 1, review.model_dump_json()),
            )
        return self.get(project, run, key)

    def _classification_status(self, key: UUID) -> str | None:
        """只读分类状态；没有记录返回 None。"""
        with self.repository.connect() as conn:
            row = conn.execute(
                "SELECT status FROM cleaning_classifications WHERE analysis_id=?", (str(key),)
            ).fetchone()
        return str(row[0]) if row else None

    def _classification_available(self, key: UUID) -> None:
        with self.repository.connect() as conn:
            saved = conn.execute(
                "SELECT status FROM cleaning_classifications WHERE analysis_id=?", (str(key),)
            ).fetchone()
        if saved and saved[0] == "outcome_pending":
            raise DomainError(
                409,
                "CLEANING_CLASSIFICATION_OUTCOME_PENDING",
                "上次自动分类结果未知，已停止自动重试以避免重复计费",
            )
        if saved and saved[0] == "running":
            raise DomainError(
                409,
                "CLEANING_CLASSIFICATION_IN_PROGRESS",
                "自动分类正在执行，请稍后刷新，勿重复提交",
            )
        if saved and saved[0] == "failed":
            raise DomainError(
                409,
                "CLEANING_CLASSIFICATION_FAILED",
                "自动分类失败，已保留汇总与清洗快照；请点击“重新自动分类并继续”",
            )

    def _claim_classification(self, key: UUID, *, retry_failed: bool) -> dict[str, object]:
        """原子抢占一次模型调用；只有显式操作可重试已知失败。"""
        document = json.dumps({"protocol_version": CLASSIFICATION_PROTOCOL})
        with self.repository.connect() as conn:
            saved = conn.execute(
                "SELECT status, document FROM cleaning_classifications WHERE analysis_id=? FOR UPDATE",
                (str(key),),
            ).fetchone()
            if saved:
                status = str(saved[0])
                if status == "failed" and retry_failed:
                    try:
                        previous = json.loads(str(saved[1]))
                        if not isinstance(previous, dict):
                            previous = {}
                    except (TypeError, ValueError):
                        previous = {}
                    conn.execute(
                        "UPDATE cleaning_classifications SET status='running', document=? "
                        "WHERE analysis_id=?",
                        (json.dumps(previous, ensure_ascii=False), str(key)),
                    )
                    return previous
                if status == "failed":
                    raise DomainError(
                        409,
                        "CLEANING_CLASSIFICATION_FAILED",
                        "自动分类失败，已保留汇总与清洗快照；请点击“重新自动分类并继续”",
                    )
                if status in {"running", "outcome_pending"}:
                    code = (
                        "CLEANING_CLASSIFICATION_IN_PROGRESS"
                        if status == "running"
                        else "CLEANING_CLASSIFICATION_OUTCOME_PENDING"
                    )
                    raise DomainError(409, code, "分类正在处理或结果未知，请勿重复提交")
                raise DomainError(409, "CLEANING_CLASSIFICATION_EXISTS", "该快照已有分类结果")
            inserted = conn.execute(
                "INSERT INTO cleaning_classifications (analysis_id,status,document) VALUES(?,?,?) ON CONFLICT (analysis_id) DO NOTHING",
                (str(key), "running", document),
            )
            claimed = conn.execute(
                "SELECT status FROM cleaning_classifications WHERE analysis_id=? FOR UPDATE",
                (str(key),),
            ).fetchone()
            if claimed is None or claimed[0] != "running":
                raise DomainError(409, "CLEANING_CLASSIFICATION_IN_PROGRESS", "自动分类正在执行，请勿重复提交")
        return {}

    def classify(
        self, project: UUID, run: UUID, key: UUID, *, retry_failed: bool = False
    ) -> CleaningReport:
        report = self.get(project, run, key)
        if report.stale:
            raise DomainError(409, "CLEANING_STALE", "上游已变更，请重新生成分析")
        # 已有审核结果时默认直接返回；只有"上次分类失败 + 显式重试"才允许覆盖重跑。
        if report.review is not None and not (
            retry_failed and self._classification_status(key) == "failed"
        ):
            return report
        roots = self.page(project, run, key, "frequencies", 0, report.frequency_count).frequencies
        if not roots:
            raise DomainError(422, "CLEANING_ROOTS_EMPTY", "没有可分类的清洗词根")
        from app.services.qwen import QwenClient, QwenConfig

        model = self.model_client or QwenClient(
            QwenConfig.load(self.repository.storage_root),
            diagnostic_dir=self.repository.storage_root / "model_diagnostics",
        )
        product = self._product(run)
        # 品类层：共享文件只保存人工维护的规则；本商品推断出的主体词按 run 单独保存。
        keywords = [
            phrase.keyword
            for phrase in self.page(project, run, key, "phrases", 0, report.phrase_count).phrases
        ]
        category = taxonomy.ensure_category(
            self.repository.storage_root,
            project,
            product.product_name,
            product.product_type,
            keywords,
            run_id=run,
        )
        features_text = " ".join(product.features)
        # 一个词根可以有多行（每行一个标签）；同一标签不重复，跨行允许重复。
        resolved: dict[str, list[RootReview]] = {}
        pinned: set[str] = set()

        def add(term: str, label: str, evidence: str) -> None:
            rows = resolved.setdefault(term, [])
            if all(row.category != label for row in rows):
                rows.append(RootReview(term=term, category=cast(Category, label), evidence=evidence))
            pinned.add(term)

        # 规则优先：词典能断定的词根全部直接定类，只有真正未命中的才调模型。
        # 主体/品牌/单位/停用词显式记为 unclassified（不进三类），模型不得再改。
        for item in roots:
            found, reason = taxonomy.classify(item.term, category)
            if found == "unclassified":
                continue  # 真正未命中，交给模型
            label = taxonomy.review_category(found)
            if label == "unclassified":
                add(item.term, "unclassified", f"词典识别为{reason}，不进三类，请在审核页确认")
                continue
            add(item.term, label, f"词典归类：{reason}")
            for extra in taxonomy.secondary_categories(item.term, features_text):
                add(item.term, extra, f"商品资料把该{reason}列为卖点，同时保留原标签")
        # 重试时先恢复上次已完成的标签，只补跑真正失败的词根，避免把成功项重发给模型。
        if retry_failed and report.review is not None:
            for row in report.review.roots:
                if row.category != "unclassified":
                    add(row.term, row.category, row.evidence)
        rule_hits = len(pinned)
        summaries: list[str] = [f"词典规则直接归类 {rule_hits} 个词根"]
        pending = [item for item in roots if item.term not in pinned]
        # 模型配置必须在占用任务前校验：失败时不写 running，任务可直接重试。
        if pending:
            model.config.validate()
        fingerprint = hashlib.sha256(json.dumps({"product": product.model_dump(mode="json"), "roots": [item.model_dump(mode="json") for item in roots], "model": model.config.model, "protocol": CLASSIFICATION_PROTOCOL, "rule": DICTIONARY}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        previous = self._claim_classification(key, retry_failed=retry_failed)
        if previous.get("fingerprint") == fingerprint:
            for raw in previous.get("roots", []):
                try:
                    saved = RootReview.model_validate(raw)
                except (TypeError, ValueError):
                    continue
                if saved.term not in pinned and saved.category != "unclassified":
                    add(saved.term, saved.category, saved.evidence)
            summaries.extend(str(item) for item in previous.get("summaries", []))
            pending = [item for item in roots if item.term not in pinned]
        # 上次"模型判定不属三类"是正常结论，保留即可；只有被记为 failed 的词才需要补跑。
        previous_failed = {str(term) for term in previous.get("failed_terms") or []}
        if retry_failed and report.review is not None:
            for row in report.review.roots:
                if row.category == "unclassified" and row.term not in previous_failed:
                    add(row.term, "unclassified", row.evidence)
            pending = [item for item in roots if item.term not in pinned]

        def flatten() -> list[dict[str, object]]:
            return [
                row.model_dump()
                for item in roots
                for row in resolved.get(item.term, [])
            ]

        progress: dict[str, object] = {"rule_version": DICTIONARY, "protocol_version": CLASSIFICATION_PROTOCOL, "model": model.config.model, "fingerprint": fingerprint, "roots": flatten(), "summaries": summaries}
        failed_terms: set[str] = set()
        last_error_code = ""
        try:
            # 首次 + 自动补跑：只重跑"没有拿到判断"的批次；模型判为未归类是正常结论，不重试。
            for attempt in range(CLASSIFICATION_ATTEMPTS):
                targets = (
                    pending if attempt == 0
                    else [item for item in pending if item.term in failed_terms]
                )
                if not targets:
                    break
                for offset in range(0, len(targets), CLASSIFICATION_BATCH_SIZE):
                    batch = targets[offset:offset + CLASSIFICATION_BATCH_SIZE]
                    batch_terms = {item.term for item in batch}
                    try:
                        if isinstance(model, QwenClient):
                            decision = model.review_cleaning(product, batch, [], diagnostic_context={"analysis_id": str(key), "run_id": str(run), "batch_offset": offset})
                        else:
                            decision = model.review_cleaning(product, batch, [])
                    except DomainError as batch_error:
                        retryable = (
                            batch_error.code in RETRYABLE_MODEL_ERRORS
                            and "鉴权" not in batch_error.message
                        )
                        if not retryable:
                            # 密钥/配置失效或付费结果未知：不再重试，保留进度并告警。
                            log.warning(
                                "cleaning classification stopped: analysis=%s code=%s",
                                key,
                                batch_error.code,
                            )
                            raise
                        # 本批没有拿到判断：暂标未分类，剩余次数内自动补跑。
                        failed_terms.update(batch_terms)
                        last_error_code = batch_error.code
                        summaries.append(
                            f"第 {offset // CLASSIFICATION_BATCH_SIZE + 1} 批 {len(batch)} 个词根本轮未取得判断（{batch_error.code}），第 {attempt + 1} 次尝试暂标为未分类"
                        )
                        for item in batch:
                            rows = resolved.setdefault(item.term, [])
                            if all(row.category != "unclassified" for row in rows):
                                rows.append(
                                    RootReview(
                                        term=item.term,
                                        category="unclassified",
                                        evidence="本轮未取得模型判断，本词根暂标记为未分类，系统将自动补跑。",
                                    )
                                )
                        progress.update(roots=flatten(), summaries=summaries)
                        with self.repository.connect() as conn:
                            conn.execute("UPDATE cleaning_classifications SET document=? WHERE analysis_id=? AND status='running'", (json.dumps(progress, ensure_ascii=False), str(key)))
                        if attempt < CLASSIFICATION_ATTEMPTS - 1:
                            time.sleep(min(2 ** attempt, 5))
                        continue
                    returned: set[str] = set()
                    for item in decision.roots:
                        # 模型只能补充未命中的词，不得覆盖词典结论，也不得新增词根。
                        if item.term not in batch_terms:
                            continue
                        returned.add(item.term)
                        rows = [
                            row for row in resolved.get(item.term, [])
                            if row.category != "unclassified"
                        ]
                        if all(row.category != item.category for row in rows):
                            rows.append(
                                RootReview(
                                    term=item.term,
                                    category=item.category,
                                    evidence=str(item.evidence or "").strip()
                                    or "模型未附证据，已按商品事实归类，请人工复核。",
                                )
                            )
                        resolved[item.term] = rows
                        failed_terms.discard(item.term)
                    for item in batch:
                        if item.term in returned:
                            continue
                        failed_terms.add(item.term)
                        rows = [
                            row for row in resolved.get(item.term, [])
                            if row.category != "unclassified"
                        ]
                        rows.append(
                            RootReview(
                                term=item.term,
                                category="unclassified",
                                evidence="模型未返回该词根，暂标记为未分类，系统将自动补跑。",
                            )
                        )
                        resolved[item.term] = rows
                    summaries.append(decision.reason)
                    progress.update(roots=flatten(), summaries=summaries)
                    with self.repository.connect() as conn:
                        conn.execute("UPDATE cleaning_classifications SET document=? WHERE analysis_id=? AND status='running'", (json.dumps(progress, ensure_ascii=False), str(key)))
                if not failed_terms:
                    break
            classified = []
            for item in roots:
                rows = resolved.get(item.term)
                if rows:
                    classified.extend(rows)
                else:
                    classified.append(
                        RootReview(
                            term=item.term,
                            category="unclassified",
                            evidence="未完成分类，请人工确认。",
                        )
                    )
            try:
                updated = self.review(
                    project,
                    run,
                    key,
                    Review(
                        expected_version=report.version,
                        roots=classified,
                        exclusions=[],
                        reason="；".join(summaries)[:1000],
                    ),
                )
            except DomainError as error:
                if error.code != "CLEANING_VERSION_CONFLICT":
                    raise
                concurrent = self.get(project, run, key)
                if concurrent.review is None or {item.term for item in concurrent.review.roots} != {
                    item.term for item in roots
                }:
                    raise
                updated = concurrent
        except DomainError as error:
            status = "outcome_pending" if error.code == "MODEL_OUTCOME_UNKNOWN" else "failed"
            with self.repository.connect() as conn:
                conn.execute(
                    "INSERT INTO cleaning_classifications (analysis_id, status, document) VALUES(?,?,?) ON CONFLICT (analysis_id) DO UPDATE SET status=excluded.status, document=excluded.document",
                    (
                        str(key),
                        status,
                        json.dumps(
                            {
                                **progress,
                                "error_code": error.code,
                                "failed_terms": sorted(failed_terms),
                            },
                            ensure_ascii=False,
                        ),
                    ),
                )
            raise
        except Exception:
            # An unexpected failure may have occurred after a paid request was sent.
            with self.repository.connect() as conn:
                conn.execute(
                    "UPDATE cleaning_classifications SET status='outcome_pending', document=? WHERE analysis_id=? AND status='running'",
                    (json.dumps({**progress, "error_code": "UNEXPECTED_ERROR"}, ensure_ascii=False), str(key)),
                )
            raise
        # 仍有词根未拿到判断：保留已出的审核结果供下游使用，但状态记为 failed，允许再次补跑。
        final_status = "failed" if failed_terms else "succeeded"
        retry_count = int(previous.get("retry_count") or 0) if previous else 0
        next_retry_at: str | None = None
        if failed_terms:
            # 延迟补跑：15 秒一次、45 秒一次；超过上限只告警，不无限调用。
            retry_count += 1
            if retry_count <= 2:
                delay = 15 if retry_count == 1 else 45
                next_retry_at = (datetime.now(UTC) + timedelta(seconds=delay)).isoformat()
            # 告警：密钥/额度或模型服务异常时需要人工介入，但进度不会丢。
            log.warning(
                "cleaning classification incomplete: analysis=%s failed=%d model=%s retry=%d",
                key,
                len(failed_terms),
                model.config.model,
                retry_count,
            )
        with self.repository.connect() as conn:
            conn.execute(
                "INSERT INTO cleaning_classifications (analysis_id, status, document) VALUES(?,?,?) ON CONFLICT (analysis_id) DO UPDATE SET status=excluded.status, document=excluded.document",
                (
                    str(key),
                    final_status,
                    json.dumps(
                        {
                            "rule_version": DICTIONARY,
                            "protocol_version": CLASSIFICATION_PROTOCOL,
                            "model": model.config.model,
                            "fingerprint": fingerprint,
                            "failed_terms": sorted(failed_terms),
                            "retry_count": retry_count,
                            "next_retry_at": next_retry_at,
                            **({"error_code": last_error_code or "MODEL_OUTPUT_INVALID"} if failed_terms else {}),
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
        return updated

    def export(self, project: UUID, run: UUID, key: UUID) -> str:
        report = self.get(project, run, key)
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(
            [
                "keyword",
                "excluded_reason",
                "source_id",
                "provider",
                "direction",
                "period_start",
                "period_end",
                "period_kind",
                "collected_at",
                "query",
                "source_row",
                "original",
                "source_evidence",
                "confirmed_roots",
                "algorithm",
                "review_version",
                "stale",
            ]
        )
        sources = {s.id: s for s in report.sources}
        exclusions = (
            {e.keyword: e.reason for e in report.review.exclusions} if report.review else {}
        )
        roots = (
            json.dumps([r.model_dump() for r in report.review.roots], ensure_ascii=False)
            if report.review
            else "[]"
        )
        for phrase in self.page(project, run, key, "phrases", 0, report.phrase_count).phrases:
            for e in phrase.evidence:
                s = sources[e.source_id]
                values = [
                    phrase.keyword,
                    exclusions.get(phrase.keyword, ""),
                    str(s.id),
                    s.provider,
                    s.direction,
                    str(s.period_start or ""),
                    str(s.period_end or ""),
                    s.period_kind,
                    s.collected_at.isoformat() if s.collected_at else "",
                    e.query,
                    str(e.source_row),
                    e.original,
                    e.payload,
                    roots,
                    report.algorithm_version,
                    str(report.version),
                    str(report.stale),
                ]
                writer.writerow(
                    ["'" + v if v.lstrip().startswith(("=", "+", "-", "@")) else v for v in values]
                )
        return stream.getvalue()
