"""第二步：周报候选、语义分析、人工确认；不修改第一步词根。"""

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal
from uuid import UUID, uuid4, uuid5

from app.db.repository import Repository
from app.domain.core_terms import (
    CoreAnalysis,
    CoreCandidate,
    CoreCandidates,
    CorePrefilter,
    CoreRequest,
    CoreSelection,
    CoreSelectionInput,
    CoreTerm,
    HotSearchCoreItem,
)
from app.domain.errors import DomainError
from app.domain.product_understanding import ProductConfirmation, UnderstandingResult
from app.domain.text import contains_root, fold_text, phrase_key, tokenize
from app.services.aba import AbaService
from app.services.qwen import QwenClient, QwenConfig

if TYPE_CHECKING:
    from app.services.hot_search_core_analysis import HotSearchCoreAnalysis

PROMPT_VERSION = "core-terms-v6"


class CoreService:
    def __init__(
        self,
        repository: Repository,
        aba: AbaService,
        client: QwenClient | None,
        *,
        hot_analysis_getter: Callable[[UUID, UUID, UUID | None], "HotSearchCoreAnalysis"]
        | None = None,
    ) -> None:
        self.repository = repository
        self.aba = aba
        self.client = client
        self.hot_analysis_getter = hot_analysis_getter

    def hot_search_candidates(
        self, project_id: UUID, run_id: UUID, report_id: UUID
    ) -> list[HotSearchCoreItem]:
        report = self.aba.get(project_id, run_id, report_id)
        if report.stale:
            raise DomainError(409, "CORE_REPORT_STALE", "商品词或商品信息已变化，请重新采集周报")
        if report.query_mode != "product_term":
            raise DomainError(409, "CORE_PRODUCT_TERM_REPORT_REQUIRED", "需要商品词热门搜索词周报")
        unique: dict[str, HotSearchCoreItem] = {}
        for query in report.reports:
            for row in query.rows:
                key = fold_text(row.keyword)
                if key in unique:
                    item = unique[key]
                    if item.search_frequency_rank != row.search_frequency_rank:
                        raise DomainError(409, "CORE_RANK_CONFLICT", "同周同词排名冲突，请重新采集")
                    if query.query not in item.source_product_terms:
                        item.source_product_terms.append(query.query)
                else:
                    unique[key] = HotSearchCoreItem(
                        candidate_id=uuid5(report.id, key),
                        keyword=" ".join(row.keyword.split()),
                        search_frequency_rank=row.search_frequency_rank,
                        source_product_terms=[query.query],
                    )
        # 冲突检查覆盖全部行，防止先过滤后掩盖超阈值的矛盾证据。
        return sorted(
            (item for item in unique.values() if item.search_frequency_rank <= 250000),
            key=lambda item: (item.search_frequency_rank, fold_text(item.keyword)),
        )

    def candidates(
        self, project_id: UUID, run_id: UUID, report_id: UUID, offset: int = 0, limit: int = 20
    ) -> CoreCandidates:
        report = self.aba.get(project_id, run_id, report_id)
        if report.stale:
            raise DomainError(409, "CORE_REPORT_STALE", "词根或商品信息已变化，请重新采集周报")
        if report.query_mode != "root":
            raise DomainError(409, "CORE_FORWARD_REPORT_REQUIRED", "第二步需要词根正向周报")
        unique: dict[str, CoreCandidate] = {}
        for query in report.reports:
            for row in query.rows:
                text = " ".join(row.keyword.split())
                parts = tokenize(text)
                matched = [root for root in report.roots_snapshot if contains_root(text, root)]
                if not matched:
                    continue
                key = text.casefold()
                if key in unique:
                    if unique[key].search_frequency_rank != row.search_frequency_rank:
                        raise DomainError(409, "CORE_RANK_CONFLICT", "同周同词排名冲突，请重新采集")
                    continue
                unique[key] = CoreCandidate(
                    id=uuid5(report.id, key),
                    keyword=text,
                    search_frequency_rank=row.search_frequency_rank,
                    matched_roots=matched,
                    root_reference=any(parts == tokenize(root) for root in matched),
                )
        items = sorted(
            unique.values(), key=lambda item: (item.search_frequency_rank, item.keyword.casefold())
        )
        return CoreCandidates(
            report_id=report_id,
            total=len(items),
            offset=offset,
            limit=limit,
            source_partial=any(q.truncated for q in report.reports),
            items=items[offset : offset + limit],
        )

    def _product(self, run_id: UUID) -> UnderstandingResult:
        with self.repository.connect() as conn:
            row = conn.execute(
                "SELECT document FROM product_understandings WHERE run_id=? AND status='succeeded'",
                (str(run_id),),
            ).fetchone()
            confirmation = conn.execute(
                "SELECT document FROM product_confirmations WHERE run_id=? ORDER BY version DESC LIMIT 1",
                (str(run_id),),
            ).fetchone()
        if not row:
            raise DomainError(409, "PRODUCT_UNDERSTANDING_REQUIRED", "请先完成商品识别")
        result = UnderstandingResult.model_validate_json(row[0])
        if confirmation:
            result.product = ProductConfirmation.model_validate_json(confirmation[0]).data.product
        if result.product.product_type.casefold() in ("unknown", "未知", "无法识别"):
            raise DomainError(409, "INSUFFICIENT_PRODUCT_EVIDENCE", "商品类型未知，无法分析相关性")
        return result

    def _save(self, result: CoreAnalysis) -> None:
        with self.repository.connect() as conn:
            conn.execute(
                "UPDATE core_analyses SET document=? WHERE id=?",
                (result.model_dump_json(), str(result.id)),
            )

    def prefilter(self, project_id: UUID, run_id: UUID, report_id: UUID) -> CorePrefilter:
        """覆盖已采集范围；每批先认领再计费，成功缓存，失败不自动重发。"""
        # 当前实际最多读取 10000 个已采集候选，再按 200 个一批粗筛；不等于采集全站词。
        # 页面“1000”文案与这里的读取上限存在差异，维护时需同时核对采集/粗筛/详审范围。
        page = self.candidates(project_id, run_id, report_id, 0, 10000)
        product = self._product(run_id)
        model = self.client or QwenClient(
            QwenConfig.load(self.repository.storage_root),
            diagnostic_dir=self.repository.storage_root / "model_diagnostics",
        )
        model.config.validate()
        selected: list[UUID] = []
        for offset in range(0, len(page.items), 200):
            batch = page.items[offset : offset + 200]
            # 商品、周报、模型、候选 ID 共同确定缓存身份；不能只按关键词文本复用旧结论。
            fingerprint = hashlib.sha256(
                json.dumps(
                    {
                        "prompt": "core-prefilter-v1",
                        "report": str(report_id),
                        "product": product.model_dump_json(),
                        "model": model.config.model,
                        "base_url": model.config.base_url,
                        "ids": [str(i.id) for i in batch],
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            with self.repository.connect() as conn:
                saved = conn.execute(
                    "SELECT status, document FROM core_prefilters WHERE run_id=? AND fingerprint=?",
                    (str(run_id), fingerprint),
                ).fetchone()
                # running/结果待确认也占用该批：网络超时不代表上游未计费，禁止自动重发。
                if saved:
                    if saved[0] != "succeeded":
                        raise DomainError(
                            409,
                            "CORE_PREFILTER_ALREADY_ATTEMPTED",
                            "该批粗筛已执行或结果待确认，不会重复调用模型",
                        )
                    selected.extend(CorePrefilter.model_validate_json(saved[1]).candidate_ids)
                    continue
                conn.execute(
                    "INSERT INTO core_prefilters VALUES (?,?,?,?)",
                    (str(run_id), fingerprint, "running", "{}"),
                )
            try:
                draft = model.prefilter_core_terms(product.product, batch)
                ids = draft.candidate_ids
                if len(ids) != len(set(ids)) or not set(ids).issubset({i.id for i in batch}):
                    raise DomainError(502, "MODEL_OUTPUT_INVALID", "粗筛候选 ID 重复或越界")
                result = CorePrefilter(candidate_ids=ids, screened_candidates=len(batch))
            except Exception as error:
                status = (
                    "failed"
                    if isinstance(error, DomainError) and error.code != "MODEL_OUTCOME_UNKNOWN"
                    else "outcome_pending"
                )
                with self.repository.connect() as conn:
                    conn.execute(
                        "UPDATE core_prefilters SET status=? WHERE run_id=? AND fingerprint=?",
                        (status, str(run_id), fingerprint),
                    )
                if isinstance(error, DomainError):
                    raise
                raise DomainError(502, "CORE_OUTCOME_UNKNOWN", "粗筛结果待确认") from None
            with self.repository.connect() as conn:
                conn.execute(
                    "UPDATE core_prefilters SET status='succeeded', document=? WHERE run_id=? AND fingerprint=?",
                    (result.model_dump_json(), str(run_id), fingerprint),
                )
            selected.extend(ids)
        if self.aba.get(project_id, run_id, report_id).stale:
            raise DomainError(409, "CORE_REPORT_STALE", "粗筛期间商品或词根已变化")
        kept = set(selected)
        return CorePrefilter(
            candidate_ids=[i.id for i in page.items if i.id in kept],
            screened_candidates=len(page.items),
        )

    def analyze(
        self,
        project_id: UUID,
        run_id: UUID,
        report_id: UUID,
        data: CoreRequest,
        *,
        retry: int = 0,
    ) -> CoreAnalysis:
        """retry 参与指纹：仅用于显式重试模型无效输出，不改变业务请求。"""
        page = self.candidates(project_id, run_id, report_id, data.offset, data.limit)
        if data.candidate_ids:
            page = self.candidates(project_id, run_id, report_id, 0, 10000)
            ids = set(data.candidate_ids)
            if len(ids) != len(data.candidate_ids) or not ids.issubset({i.id for i in page.items}):
                raise DomainError(422, "INVALID_CORE_CANDIDATES", "候选 ID 重复或不属于当前周报")
            page.items = [i for i in page.items if i.id in ids]
        if not page.items:
            raise DomainError(409, "CORE_NO_CANDIDATES", "此页没有可分析候选")
        product = self._product(run_id)
        model = self.client or QwenClient(
            QwenConfig.load(self.repository.storage_root),
            diagnostic_dir=self.repository.storage_root / "model_diagnostics",
        )
        model.config.validate()
        # 品牌顺序、大小写不应引起相同语义请求重复计费。
        data = data.model_copy(
            update={"own_brands": sorted({brand.casefold() for brand in data.own_brands})}
        )
        context = product.model_dump_json()

        def fingerprint_for(prompt: str, attempt: int) -> str:
            return hashlib.sha256(
                json.dumps(
                    {
                        "report": str(report_id),
                        "context": context,
                        "request": data.model_dump(mode="json"),
                        "model": model.config.model,
                        "base_url": model.config.base_url,
                        "prompt": prompt,
                        "retry": attempt,
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest()

        fingerprint = fingerprint_for(PROMPT_VERSION, retry)
        result = CoreAnalysis(
            id=uuid4(),
            project_id=project_id,
            run_id=run_id,
            report_id=report_id,
            status="running",
            model=model.config.model,
            prompt_version=PROMPT_VERSION,
            created_at=datetime.now(UTC),
            request=data,
            total_candidates=page.total,
            remaining_candidates=page.total - len(page.items),
            source_partial=page.source_partial,
            warnings=[
                "品牌归属为模型建议，仅供参考；品牌词无需确认，可不填写。",
                "只分析当前页；未分析记录不代表不相关。",
            ]
            + (["周报采集不完整，候选仅覆盖已采集范围。"] if page.source_partial else []),
        )
        with self.repository.connect() as conn:
            # 唯一键和写锁认领计费请求；释放事务后再调用模型。
            if PROMPT_VERSION == "core-terms-v6":
                # v6 仅更换模型端编号协议，v5 的同上下文成功判断仍有效。
                old_rows = conn.execute(
                    "SELECT document FROM core_analyses WHERE run_id=? AND fingerprint IN (?,?,?)",
                    (str(run_id), *(fingerprint_for("core-terms-v5", i) for i in range(3))),
                ).fetchall()
                previous_batches = [CoreAnalysis.model_validate_json(row[0]) for row in old_rows]
                success = next(
                    (item for item in previous_batches if item.status == "succeeded"), None
                )
                if success is not None:
                    return self.get(project_id, run_id, success.id)
                if any(item.status in ("running", "outcome_pending") for item in previous_batches):
                    raise DomainError(
                        409,
                        "CORE_ANALYSIS_ALREADY_ATTEMPTED",
                        "原协议批次仍在处理或结果待确认，不会重复调用模型",
                    )
            saved = conn.execute(
                "SELECT document FROM core_analyses WHERE run_id=? AND fingerprint=?",
                (str(run_id), fingerprint),
            ).fetchone()
            if saved:
                previous = CoreAnalysis.model_validate_json(saved[0])
                if previous.status == "succeeded":
                    return self.get(project_id, run_id, previous.id)
                if previous.status == "failed":
                    raise DomainError(
                        409,
                        previous.error_code or "CORE_ANALYSIS_FAILED",
                        previous.error_message
                        or f"该批上次分析失败：{previous.error_code}；未重复调用模型",
                    )
                raise DomainError(
                    409,
                    "CORE_ANALYSIS_ALREADY_ATTEMPTED",
                    "该批分析已执行或结果待确认，请读取状态；不会重复调用模型",
                )
            conn.execute(
                "INSERT INTO core_analyses (id, run_id, fingerprint, report_id, document) VALUES (?,?,?,?,?)",
                (
                    str(result.id),
                    str(run_id),
                    fingerprint,
                    str(report_id),
                    result.model_dump_json(),
                ),
            )
        try:
            draft = model.analyze_core_terms(product.product, page.items, data.own_brands)
            ids = [item.candidate_id for item in draft.items]
            if len(ids) != len(set(ids)) or set(ids) != {item.id for item in page.items}:
                raise DomainError(502, "MODEL_OUTPUT_INVALID", "模型候选 ID 缺失、重复或越界")
            judgments = {item.candidate_id: item for item in draft.items}
            for candidate in page.items:
                judgment = judgments[candidate.id]
                category = judgment.category
                explicit_brand = any(
                    contains_root(candidate.keyword, brand) for brand in data.own_brands
                )
                if explicit_brand:
                    category = "own_brand"
                elif category == "own_brand":
                    category = "brand_unknown"
                result.items.append(
                    CoreTerm(
                        **candidate.model_dump(),
                        **judgment.model_dump(exclude={"candidate_id", "category"}),
                        category=category,
                        # 与商品一致（exact）或覆盖更宽但仍可投放的品类（broad）都可作核心词；
                        # 配件和无关词不推荐，词根本身也不重复当核心词。
                        recommended=judgment.relevance == "high"
                        and judgment.intent in ("exact", "broad")
                        and category == "generic"
                        and judgment.keyword_role == "core"
                        and not candidate.root_reference,
                    )
                )
            result.status = "succeeded"
            # 结果属于原周报；输入变化时留作历史，不覆盖人工选择。
            result.stale = self.aba.get(project_id, run_id, report_id).stale
            self._save(result)
            return result
        except DomainError as error:
            result.status = "outcome_pending" if error.code == "MODEL_OUTCOME_UNKNOWN" else "failed"
            result.error_code = error.code
            result.error_message = error.message
            self._save(result)
            raise
        except Exception:
            # 未知异常可能发生在发送后，宁可保留不确定状态也不默默重复计费。
            result.status = "outcome_pending"
            result.error_code = "CORE_OUTCOME_UNKNOWN"
            self._save(result)
            raise DomainError(502, "CORE_OUTCOME_UNKNOWN", "分析结果待确认，请读取状态") from None

    def get(self, project_id: UUID, run_id: UUID, analysis_id: UUID) -> CoreAnalysis:
        self.repository.get_run(project_id, run_id)
        with self.repository.connect() as conn:
            row = conn.execute(
                "SELECT document FROM core_analyses WHERE id=? AND run_id=?",
                (str(analysis_id), str(run_id)),
            ).fetchone()
        if not row:
            raise DomainError(404, "CORE_ANALYSIS_NOT_FOUND", "未找到此任务的分析记录")
        result = CoreAnalysis.model_validate_json(row[0])
        result.stale = self.aba.get(project_id, run_id, result.report_id).stale
        return result

    def history(self, project_id: UUID, run_id: UUID, report_id: UUID) -> list[CoreAnalysis]:
        self.aba.get(project_id, run_id, report_id)
        with self.repository.connect() as conn:
            rows = conn.execute(
                "SELECT id FROM core_analyses WHERE run_id=? AND report_id=? ORDER BY seq DESC LIMIT 50",
                (str(run_id), str(report_id)),
            ).fetchall()
        return [self.get(project_id, run_id, UUID(row[0])) for row in rows]

    def selection(self, project_id: UUID, run_id: UUID) -> CoreSelection:
        self.repository.get_run(project_id, run_id)
        with self.repository.connect() as conn:
            row = conn.execute(
                "SELECT document FROM core_selections WHERE run_id=? ORDER BY version DESC LIMIT 1",
                (str(run_id),),
            ).fetchone()
        if not row:
            raise DomainError(404, "CORE_SELECTION_NOT_FOUND", "尚未确认核心大词")
        result = CoreSelection.model_validate_json(row[0])
        if result.rule_version in ("hot-search-core-v1", "hot-search-core-v2"):
            try:
                analysis = self._current_hot_analysis(project_id, run_id, result.analysis_id)
                result.stale = analysis.stale
            except DomainError as error:
                if error.status_code not in (404, 422):
                    raise
                result.stale = True
        else:
            result.stale = self.get(project_id, run_id, result.analysis_id).stale
        return result

    def _current_hot_analysis(
        self, project_id: UUID, run_id: UUID, analysis_id: UUID
    ) -> "HotSearchCoreAnalysis":
        if self.hot_analysis_getter is None:
            raise DomainError(422, "CORE_ANALYSIS_NOT_CURRENT", "热门搜索词分析服务未配置")
        analysis = self.hot_analysis_getter(project_id, run_id, None)
        if (
            analysis.id != analysis_id
            or analysis.rule_version not in ("hot-search-core-v1", "hot-search-core-v2")
            or analysis.stale
            or analysis.status not in ("completed", "incomplete")
        ):
            raise DomainError(422, "CORE_ANALYSIS_NOT_CURRENT", "请选择当前有效的终态分析")
        return analysis

    def aggregate(
        self,
        project_id: UUID,
        run_id: UUID,
        keys: list[UUID],
        *,
        screened_candidates: int = 0,
        relevant_candidates: int = 0,
    ) -> CoreAnalysis:
        """合并同一周报成功批次，保留来源 ID；不产生额外模型调用。"""
        analyses = [self.get(project_id, run_id, key) for key in dict.fromkeys(keys)]
        if not analyses or any(a.stale or a.status != "succeeded" for a in analyses):
            raise DomainError(409, "CORE_ANALYSIS_NOT_CURRENT", "汇总批次须全部有效且分析成功")
        first = analyses[0]
        if any(
            a.report_id != first.report_id
            or a.source_analysis_ids
            or a.request.own_brands != first.request.own_brands
            for a in analyses
        ):
            raise DomainError(
                422, "INVALID_CORE_SELECTION", "只能汇总同一周报、相同品牌设置的原始批次"
            )
        unique: dict[str, CoreTerm] = {}
        for item in sorted(
            (i for a in analyses for i in a.items),
            key=lambda i: (not i.recommended, i.search_frequency_rank, i.keyword),
        ):
            unique.setdefault(phrase_key(item.keyword), item)
        items = sorted(unique.values(), key=lambda i: (i.search_frequency_rank, i.keyword))
        fingerprint = (
            "aggregate-v1:"
            + hashlib.sha256(
                (
                    ",".join(str(a.id) for a in analyses)
                    + f":{screened_candidates}:{relevant_candidates}"
                ).encode()
            ).hexdigest()
        )
        with self.repository.connect() as conn:
            if self.aba.get(project_id, run_id, first.report_id).stale:
                raise DomainError(409, "CORE_ANALYSIS_NOT_CURRENT", "汇总期间商品或词根已变化")
            old = conn.execute(
                "SELECT document FROM core_analyses WHERE run_id=? AND fingerprint=?",
                (str(run_id), fingerprint),
            ).fetchone()
            if old:
                return CoreAnalysis.model_validate_json(old[0])
            result = first.model_copy(
                deep=True,
                update={
                    "id": uuid4(),
                    "created_at": datetime.now(UTC),
                    "items": items,
                    "source_analysis_ids": [a.id for a in analyses],
                    "screened_candidates": screened_candidates,
                    "relevant_candidates": relevant_candidates,
                    "remaining_candidates": max(0, first.total_candidates - len(items)),
                    "warnings": [
                        f"已汇总 {len(analyses)} 批、{len(items)} 个去重候选；仅覆盖设定分析范围。",
                        "属性长尾保留在审核结果中，不计入核心品类词。",
                    ]
                    + (
                        [
                            f"已粗筛 {screened_candidates} 个候选，保留 {relevant_candidates} 个相关词，"
                            f"详细审核 {len(items)} 个，尚余 {max(0, relevant_candidates - len(items))} 个相关词未详审。"
                        ]
                        if screened_candidates
                        else []
                    )
                    + (["周报采集不完整，仅覆盖已采集范围。"] if first.source_partial else []),
                },
            )
            conn.execute(
                "INSERT INTO core_analyses (id, run_id, fingerprint, report_id, document) VALUES (?,?,?,?,?)",
                (
                    str(result.id),
                    str(run_id),
                    fingerprint,
                    str(first.report_id),
                    result.model_dump_json(),
                ),
            )
        return result

    @staticmethod
    def _hot_selection_items(source: "HotSearchCoreAnalysis", ids: set[UUID]) -> list[CoreTerm]:
        from app.services.hot_search_core_rules import is_core_eligible, sort_core_items

        manual_fallback = not source.recommended_candidate_ids

        def selectable(item: HotSearchCoreItem) -> bool:
            return item.search_frequency_rank <= 250000 and (
                manual_fallback or is_core_eligible(item, source.rule_version)
            )

        def relevance(item: HotSearchCoreItem) -> Literal["high", "medium", "low"]:
            percent = item.summary.relevance_percent if item.summary is not None else None
            if percent is None or percent <= 0:
                return "low"
            threshold = 50 if source.rule_version == "hot-search-core-v2" else 60
            passes = (
                percent >= threshold
                if source.rule_version == "hot-search-core-v2"
                else percent > threshold
            )
            return "high" if passes else "medium"

        items = [
            CoreTerm(
                id=item.candidate_id,
                keyword=item.keyword,
                search_frequency_rank=item.search_frequency_rank,
                matched_roots=[],
                root_reference=False,
                source_product_terms=item.source_product_terms,
                intent_evidence=item.intent_evidence,
                summary=item.summary,
                comparisons=item.comparisons,
                translation="",
                relevance=relevance(item),
                intent=item.intent_evidence.category if item.intent_evidence else "unrelated",
                category=item.brand_category,
                reason=(
                    "人工筛选确认：ABA 排名在 250,000 以内，自动门槛未选出核心词"
                    if manual_fallback
                    else "热门搜索词精准意图及四维相关度门槛通过"
                ),
                recommended=not manual_fallback,
            )
            for item in sort_core_items(source.items, source.rule_version)
            if item.candidate_id in ids and selectable(item)
        ]
        if len(items) != len(ids):
            message = (
                "自动无合格项时，只能人工确认本次分析中 ABA 排名 250,000 以内的候选"
                if manual_fallback
                else "只能确认本次分析中符合条件的核心大词"
            )
            raise DomainError(422, "INVALID_CORE_SELECTION", message)
        return items

    def select(self, project_id: UUID, run_id: UUID, data: CoreSelectionInput) -> CoreSelection:
        ids = set(data.candidate_ids)
        hot_analysis = data.rule_version in ("hot-search-core-v1", "hot-search-core-v2")
        if hot_analysis:
            source = self._current_hot_analysis(project_id, run_id, data.analysis_id)
            analysis_id, report_id = source.id, source.report_id
            items = self._hot_selection_items(source, ids)
        else:
            analysis = self.get(project_id, run_id, data.analysis_id)
            if analysis.stale or analysis.status != "succeeded":
                raise DomainError(409, "CORE_ANALYSIS_NOT_CURRENT", "请选择有效的成功分析")
            analysis_id, report_id = analysis.id, analysis.report_id
            brands = [
                item for item in analysis.items if item.id in ids and item.category != "generic"
            ]
            if brands:
                raise DomainError(
                    422,
                    "INVALID_CORE_SELECTION",
                    "品牌词仅作参考，无需确认、可不填写；请选择通用核心大词",
                )
            items = [item for item in analysis.items if item.id in ids and item.recommended]
        if len(items) != len(ids):
            raise DomainError(422, "INVALID_CORE_SELECTION", "只能确认本次分析中符合条件的核心大词")
        with self.repository.connect() as conn:
            # 在写锁内重验周报快照，避免分析后用户已切换词根。
            if hot_analysis:
                source = self._current_hot_analysis(project_id, run_id, analysis_id)
                items = self._hot_selection_items(source, ids)
            elif self.aba.get(project_id, run_id, report_id).stale:
                raise DomainError(409, "CORE_ANALYSIS_NOT_CURRENT", "商品或词根已变化")
            saved = conn.execute(
                "SELECT document FROM core_selections WHERE run_id=? ORDER BY version DESC LIMIT 1",
                (str(run_id),),
            ).fetchone()
            previous = CoreSelection.model_validate_json(saved[0]) if saved else None
            if (
                previous
                and previous.analysis_id == analysis_id
                and previous.rule_version == data.rule_version
                and {item.id for item in previous.items} == ids
            ):
                return previous
            version = previous.version if previous else 0
            if version != data.expected_version:
                raise DomainError(409, "CORE_SELECTION_CONFLICT", "确认版本已变化，请刷新后重试")
            result = CoreSelection(
                id=uuid4(),
                project_id=project_id,
                run_id=run_id,
                analysis_id=analysis_id,
                rule_version=data.rule_version,
                source_analysis_id=analysis_id if hot_analysis else None,
                report_id=report_id,
                version=version + 1,
                confirmed_at=datetime.now(UTC),
                items=items,
            )
            conn.execute(
                "INSERT INTO core_selections VALUES (?,?,?,?)",
                (str(result.id), str(run_id), result.version, result.model_dump_json()),
            )
        return result
