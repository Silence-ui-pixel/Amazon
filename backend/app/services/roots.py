"""词根推荐与确认用例；模型仅推荐，人工选择独立存储。"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.db.repository import Repository
from app.db.scope import ConnectionScope
from app.domain.errors import DomainError
from app.domain.product_understanding import ProductUnderstanding
from app.domain.roots import (
    LANGUAGES,
    RootCandidate,
    RootCandidates,
    RootCandidatesEdit,
    RootCandidateVersion,
    RootSelection,
    RootSelectionInput,
)
from app.domain.text import fold_text
from app.services.product_confirmation import ProductConfirmationService
from app.services.product_term_quality import validate_candidates
from app.services.product_understanding import ProductUnderstandingService
from app.services.qwen import QwenClient, QwenConfig


class RootService:
    def __init__(
        self,
        repository: Repository,
        understanding: ProductUnderstandingService,
        client: QwenClient | None,
    ) -> None:
        self.repository = repository
        self.understanding = understanding
        self.client = client

    def get(self, project_id: UUID, run_id: UUID) -> RootCandidates:
        self.repository.get_run(project_id, run_id)
        with self.repository.connect() as conn:
            row = conn.execute(
                "SELECT document FROM root_candidates WHERE run_id=? AND document IS NOT NULL",
                (str(run_id),),
            ).fetchone()
        if row is None:
            raise DomainError(404, "ROOT_CANDIDATES_NOT_FOUND", "尚无已生成的商品词候选")
        return RootCandidates.model_validate_json(row[0])

    def _assert_editable(self, conn: ConnectionScope, run_id: UUID) -> None:
        # 与分析启动共用任务行锁；网络调用期间依靠持久化 running 状态阻止并发修改。
        conn.execute("SELECT id FROM pipeline_runs WHERE id=? FOR NO KEY UPDATE", (str(run_id),))
        if conn.execute(
            "SELECT 1 FROM root_candidates WHERE run_id=? AND status IN ('running','uncertain')",
            (str(run_id),),
        ).fetchone():
            raise DomainError(409, "ROOT_GENERATION_PENDING", "候选正在生成或结果待确认，请勿重复操作")
        if conn.execute(
            "SELECT 1 FROM product_term_analyses WHERE run_id=? "
            "AND document::jsonb->>'status' IN ('queued','running') LIMIT 1", (str(run_id),),
        ).fetchone() or conn.execute(
            "SELECT 1 FROM workflow_runs WHERE run_id=? AND status='running' LIMIT 1", (str(run_id),),
        ).fetchone():
            raise DomainError(409, "PRODUCT_TERM_ANALYSIS_RUNNING", "分析或流程正在运行，请在暂停或结束后修改候选")
        if conn.execute("SELECT 1 FROM aba_reports WHERE run_id=? LIMIT 1", (str(run_id),)).fetchone() or conn.execute(
            "SELECT 1 FROM core_selections WHERE run_id=? LIMIT 1", (str(run_id),)
        ).fetchone():
            raise DomainError(409, "ROOT_DOWNSTREAM_EXISTS", "本研究已产生核心词资料；请新建研究后修改商品词，避免历史资料错配")

    def _product(self, project_id: UUID, run_id: UUID) -> ProductUnderstanding:
        understanding = self.understanding.get(project_id, run_id)
        try:
            return ProductConfirmationService(self.repository, self.understanding).get(project_id, run_id).data.product
        except DomainError as error:
            if error.code != "PRODUCT_CONFIRMATION_NOT_FOUND":
                raise
        return understanding.product

    def revise(self, project_id: UUID, run_id: UUID, data: RootCandidatesEdit) -> RootCandidates:
        self.repository.get_run(project_id, run_id)
        product = self._product(project_id, run_id)
        accepted, issues = validate_candidates(data.candidates, product)
        rejected = [issue for issue in issues if issue.code != "DUPLICATE"]
        if rejected or not accepted:
            raise DomainError(422, "PRODUCT_TERM_QUALITY_INVALID", "；".join(f"{i.text}: {i.message}" for i in rejected) or "请输入准确商品词")
        with self.repository.connect() as conn:
            self._assert_editable(conn, run_id)
            previous = self.get(project_id, run_id)
            if previous.version != data.expected_version:
                raise DomainError(409, "ROOT_VERSION_CONFLICT", "候选已更新，请刷新页面后重新修改")
            snapshot = RootCandidateVersion.model_validate(previous.model_dump(exclude={"history"}))
            known_ids = {fold_text(item.text): item.id for item in previous.candidates}
            result = previous.model_copy(update={
                "version": previous.version + 1, "source": "manual", "created_at": datetime.now(UTC),
                "candidates": [RootCandidate(id=known_ids.get(fold_text(item.text), uuid4()), **item.model_dump()) for item in accepted],
                "validation_issues": issues, "raw_response": "", "diagnostic_id": "",
                "history": [*previous.history, snapshot], "rule_version": "product-term-v1",
            })
            conn.execute("UPDATE root_candidates SET document=? WHERE run_id=?", (result.model_dump_json(), str(run_id)))
            conn.execute("DELETE FROM root_selections WHERE run_id=?", (str(run_id),))
        return result

    def generate(self, project_id: UUID, run_id: UUID, *, expected_version: int | None = None) -> RootCandidates:
        run = self.repository.get_run(project_id, run_id)
        try:
            existing = self.get(project_id, run_id)
            if expected_version is None:
                return existing
        except DomainError as error:
            if error.code != "ROOT_CANDIDATES_NOT_FOUND":
                raise
        try:
            understanding = self.understanding.get(project_id, run_id)
        except DomainError as error:
            if error.code == "UNDERSTANDING_NOT_FOUND":
                raise DomainError(
                    409, "PRODUCT_UNDERSTANDING_REQUIRED", "请先完成商品识别，再生成词根候选"
                ) from None
            raise
        client = self.client or QwenClient(QwenConfig.load(self.repository.storage_root), diagnostic_dir=self.repository.storage_root / "model_diagnostics")
        client.config.validate()
        with self.repository.connect() as conn:
            conn.execute("SELECT id FROM pipeline_runs WHERE id=? FOR NO KEY UPDATE", (str(run_id),))
            if expected_version is not None:
                self._assert_editable(conn, run_id)
            row = conn.execute(
                "SELECT status,document FROM root_candidates WHERE run_id=?", (str(run_id),)
            ).fetchone()
            previous = RootCandidates.model_validate_json(row[1]) if row and row[1] else None
            if expected_version is not None and (previous is None or previous.version != expected_version):
                raise DomainError(409, "ROOT_VERSION_CONFLICT", "候选已更新，请刷新页面后重新生成")
            if row and row[0] == "succeeded" and expected_version is None:
                return RootCandidates.model_validate_json(row[1])
            if row and row[0] in ("running", "uncertain"):
                raise DomainError(
                    409, "ROOT_GENERATION_PENDING", "词根正在生成或上次结果待确认，请勿重复调用"
                )
            # 在占用写锁后读取有效人工资料，避免与商品纠错并发时使用旧上下文。
            product = understanding.product
            try:
                product = (
                    ProductConfirmationService(self.repository, self.understanding)
                    .get(project_id, run_id)
                    .data.product
                )
            except DomainError as error:
                if error.code != "PRODUCT_CONFIRMATION_NOT_FOUND":
                    raise
            if product.product_type.strip().casefold() in ("未知", "unknown", "无法识别"):
                raise DomainError(
                    422,
                    "INSUFFICIENT_PRODUCT_EVIDENCE",
                    "商品类型尚未识别，请先人工修正确认商品类型",
                )
            # 事务占用后释放数据库锁，网络请求期间不阻塞其他输入操作。
            conn.execute(
                "INSERT INTO root_candidates (run_id,status,document) VALUES (?,'running',NULL) ON CONFLICT(run_id) DO UPDATE SET status='running'",
                (str(run_id),),
            )
        try:
            draft = client.recommend_roots(run.input.marketplace, product)
            known_ids = {fold_text(item.text): item.id for item in previous.candidates} if previous else {}
            result = RootCandidates(
                project_id=project_id,
                run_id=run_id,
                model=client.config.model,
                marketplace=run.input.marketplace,
                language=LANGUAGES[run.input.marketplace],
                created_at=datetime.now(UTC),
                candidates=[
                    RootCandidate(id=known_ids.get(fold_text(item.text), uuid4()), **item.model_dump()) for item in draft.candidates
                ],
                warnings=[
                    "候选商品词将自动按前30个自然商品的四维相关度分析，无需逐项确认启动。",
                    "中文翻译和推荐理由需人工核对，不代表搜索量、流量或相关度结论。",
                ],
                rule_version="product-term-v1",
                version=previous.version + 1 if previous else 1,
                history=[*previous.history, RootCandidateVersion.model_validate(previous.model_dump(exclude={"history"}))] if previous else [],
                validation_issues=draft.validation_issues,
                raw_response=draft.raw_response,
                diagnostic_id=draft.diagnostic_id,
            )
            with self.repository.connect() as conn:
                conn.execute(
                    "UPDATE root_candidates SET status='succeeded',document=? WHERE run_id=?",
                    (result.model_dump_json(), str(run_id)),
                )
                if previous is not None:
                    conn.execute("DELETE FROM root_selections WHERE run_id=?", (str(run_id),))
            return result
        except DomainError as error:
            state = "uncertain" if error.code == "MODEL_OUTCOME_UNKNOWN" else ("succeeded" if previous else "failed")
            with self.repository.connect() as conn:
                conn.execute(
                    "UPDATE root_candidates SET status=? WHERE run_id=?", (state, str(run_id))
                )
            raise

    def get_selection(self, project_id: UUID, run_id: UUID) -> RootSelection:
        self.repository.get_run(project_id, run_id)
        with self.repository.connect() as conn:
            row = conn.execute(
                "SELECT document FROM root_selections WHERE run_id=?", (str(run_id),)
            ).fetchone()
        if row is None:
            raise DomainError(404, "ROOT_SELECTION_NOT_FOUND", "尚未确认词根")
        result = RootSelection.model_validate_json(row[0])
        candidates = self.get(project_id, run_id)
        if not set(result.candidate_ids) <= {item.id for item in candidates.candidates}:
            raise DomainError(404, "ROOT_SELECTION_NOT_FOUND", "旧版本选择已失效，请验证当前候选")
        return result

    def select(self, project_id: UUID, run_id: UUID, data: RootSelectionInput) -> RootSelection:
        candidates = self.get(project_id, run_id)
        allowed = {candidate.id for candidate in candidates.candidates}
        if not set(data.candidate_ids) <= allowed:
            raise DomainError(422, "INVALID_ROOT_SELECTION", "只能选择当前任务已生成的词根候选")
        # 按候选顺序规范化选择；重复保存相同集合复用确认时间。
        ordered = [c.id for c in candidates.candidates if c.id in data.candidate_ids]
        with self.repository.connect() as conn:
            conn.execute("SELECT id FROM pipeline_runs WHERE id=? FOR NO KEY UPDATE", (str(run_id),))
            current = self.get(project_id, run_id)
            if current.version != candidates.version:
                raise DomainError(409, "ROOT_VERSION_CONFLICT", "候选已更新，请刷新后选择")
            if candidates.rule_version == "product-term-v1":
                from app.domain.product_terms import ProductTermAnalysis
                from app.services.product_term_scoring import choose_terms
                evidence = conn.execute("SELECT document FROM product_term_analyses WHERE run_id=? ORDER BY seq DESC LIMIT 1", (str(run_id),)).fetchone()
                result_analysis = ProductTermAnalysis.model_validate_json(evidence[0]) if evidence else None
                if result_analysis is None or result_analysis.status in ("queued", "running", "stale") or result_analysis.candidate_version != candidates.version or not set(ordered) <= set(choose_terms(result_analysis.items)):
                    raise DomainError(422, "PRODUCT_TERM_VALIDATION_REQUIRED", "所选词必须先完成真实样本验证，且高相关商品比例严格大于40%")
            row = conn.execute(
                "SELECT document FROM root_selections WHERE run_id=?", (str(run_id),)
            ).fetchone()
            if row:
                saved = RootSelection.model_validate_json(row[0])
                if saved.candidate_ids == ordered and saved.source == "manual":
                    return saved
            result = RootSelection(
                project_id=project_id,
                run_id=run_id,
                candidate_ids=ordered,
                confirmed_at=datetime.now(UTC),
            )
            conn.execute(
                "INSERT INTO root_selections (run_id,document) VALUES (?,?) ON CONFLICT(run_id) DO UPDATE SET document=excluded.document",
                (str(run_id), result.model_dump_json()),
            )
        return result
