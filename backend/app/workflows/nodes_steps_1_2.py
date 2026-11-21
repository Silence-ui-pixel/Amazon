"""第 1–2 步节点：商品识别、确认、词根、报告周、ABA 周报、核心大词门。"""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import cast
from uuid import UUID

from langgraph.types import interrupt

from app.domain.aba import AbaRequest
from app.domain.core_terms import CoreAnalysis, CoreRequest, CoreSelection, CoreSelectionInput
from app.domain.errors import DomainError
from app.domain.product_terms import ProductProfileRequest
from app.domain.product_understanding import ProductConfirmationRequest
from app.domain.roots import RootSelection
from app.services.search_policy import PAUSE_CODES
from app.workflows import gates
from app.workflows.deps import WorkflowDeps
from app.workflows.errors import failure
from app.workflows.nodes_common import (
    Node,
    auto_decide,
    await_fact,
    human_payload,
    ids,
    latest_saturday,
    option_int,
    own_brands,
    week_end,
)
from app.workflows.state import WorkflowState, record


def _candidate_ref(selection: RootSelection) -> str:
    return ",".join(str(item) for item in selection.candidate_ids)


def _week_value(value: object) -> date | None:
    if not isinstance(value, dict):
        return None
    raw = cast(dict[str, object], value).get("aba_week_end")
    if not isinstance(raw, str):
        return None
    try:
        parsed = date.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.weekday() != 5 or parsed >= datetime.now(UTC).date():
        return None
    return parsed


def build_nodes_1_2(deps: WorkflowDeps) -> dict[str, Node]:
    def ensure_input(state: WorkflowState) -> dict[str, object]:
        project, run = ids(state)
        current = deps.repository.get_run(project, run)
        return record(state, "ensure_input", "done", f"站点 {current.input.marketplace}")

    def identify_product(state: WorkflowState) -> dict[str, object]:
        project, run = ids(state)
        existing = gates.understanding(deps, project, run)
        if existing is not None:
            return record(state, "identify_product", "skipped", "已有商品识别结果")
        # 已有词根说明识别早已完成；中途启动编排时不再重复调用模型。
        if gates.roots_generated(deps, project, run):
            return record(state, "identify_product", "skipped", "已有词根结果")
        try:
            result = deps.understanding.identify(project, run)
        except DomainError as error:
            return failure(state, "identify_product", error)
        return record(state, "identify_product", "done", result.product.product_type)

    def product_confirmation(state: WorkflowState) -> dict[str, object]:
        project, run = ids(state)
        existing = gates.confirmation(deps, project, run)
        if existing is not None:
            return record(
                state,
                "product_confirmation",
                "skipped",
                "已确认商品资料",
                product_confirmation_version=existing.version,
            )
        # 已选词根说明商品资料已被接受；此后再写确认会被业务层锁定。
        selection = gates.root_selection(deps, project, run)
        if selection is not None:
            return record(
                state,
                "product_confirmation",
                "skipped",
                "已有词根选择，视为已确认",
                root_selection_id=_candidate_ref(selection),
            )
        if auto_decide(state):
            current = gates.understanding(deps, project, run)
            if current is None:
                return failure(
                    state,
                    "product_confirmation",
                    DomainError(409, "PRODUCT_UNDERSTANDING_REQUIRED", "尚无商品识别结果"),
                )
            try:
                saved = deps.confirmation.save(
                    project,
                    run,
                    ProductConfirmationRequest(
                        expected_version=0,
                        product=current.product,
                        reviewer="自动编排",
                        reason="采用模型识别结果",
                    ),
                )
            except DomainError as error:
                return failure(state, "product_confirmation", error)
            return record(
                state,
                "product_confirmation",
                "done",
                "已按模型识别结果自动确认",
                product_confirmation_version=saved.version,
            )
        payload = human_payload(
            "product_confirmation",
            "确认商品识别结果",
            "在商品确认面板核对名称、类型和特征，保存后再继续。",
            refs={"run": str(run)},
        )
        confirmed = await_fact(lambda: gates.confirmation(deps, project, run), payload)
        return record(
            state,
            "product_confirmation",
            "done",
            "已保存商品确认",
            product_confirmation_version=confirmed.version,
        )

    def generate_roots(state: WorkflowState) -> dict[str, object]:
        project, run = ids(state)
        if gates.roots_generated(deps, project, run):
            return record(state, "generate_roots", "skipped", "已有词根候选")
        try:
            profile = gates.product_profile(deps, project, run)
            if profile is None:
                draft = deps.profiles.generate(project, run)
                if auto_decide(state):
                    profile = deps.profiles.confirm(
                        project,
                        run,
                        ProductProfileRequest(
                            expected_version=draft.version,
                            purpose=draft.profile.purpose,
                            style_structure=draft.profile.style_structure,
                            specifications=draft.profile.specifications,
                            package_quantity=draft.profile.package_quantity or Decimal("1"),
                            unit_basis=draft.profile.unit_basis or "each",
                            reference_price=(
                                draft.profile.price
                                if draft.profile.price_source == "user_reference"
                                else None
                            ),
                            currency=draft.profile.currency or "USD",
                        ),
                    )
                else:
                    payload = human_payload(
                        "product_profile",
                        "确认商品四维基准",
                        "请在商品四维基准面板确认用途、款式结构、规格和可靠售价。",
                        refs={"run": str(run)},
                    )
                    await_fact(lambda: gates.product_profile(deps, project, run), payload)
            result = deps.roots.generate(project, run)
        except DomainError as error:
            return failure(state, "generate_roots", error)
        return record(state, "generate_roots", "done", f"{len(result.candidates)} 个候选")

    def root_selection(state: WorkflowState) -> dict[str, object]:
        project, run = ids(state)
        current = gates.root_selection(deps, project, run)
        if current is not None:
            return record(
                state,
                "root_selection",
                "skipped",
                "已选词根",
                root_selection_id=_candidate_ref(current),
            )
        if auto_decide(state):
            # interrupt 恢复后会继续执行，必须循环重试：仍被拦截就再次暂停，
            # 不能把“恢复了但没有采到数据”当作成功。
            while True:
                try:
                    analysis = deps.term_analysis.start(project, run)
                except DomainError as error:
                    if error.code.startswith("AGENT_"):
                        return failure(state, "root_selection", error)
                    if error.code not in PAUSE_CODES and error.code != "PRODUCT_TERM_ANALYSIS_RUNNING":
                        return failure(state, "root_selection", error)
                    # 访问检查/限流需要人工在浏览器窗口处理；恢复后只重采未完成的候选。
                    interrupt(
                        human_payload(
                            "product_term_analysis",
                            "等待商品词验证" if error.code == "PRODUCT_TERM_ANALYSIS_RUNNING" else "处理 Amazon 访问检查",
                            f"{error.message}；处理完成后点击“重新检查本步，继续运行”。",
                            refs={"run": str(run)},
                        )
                    )
                    continue
                if analysis.best_candidate_ids:
                    break
                finished = sum(item.status in ("completed", "no_samples", "failed") for item in analysis.items)
                interrupt(human_payload(
                    "product_term_analysis",
                    "补充准确商品词并验证",
                    f"{finished} 个词已处理，0 个达到高相关商品比例严格大于40%的门槛。"
                    "请在候选管理中修改或重新生成完整品类词，验证通过后继续。已确认商品资料和历史分析会保留。",
                    refs={"run": str(run), "analysis_id": str(analysis.id), "candidate_version": str(analysis.candidate_version)},
                ))
            selection = deps.roots.get_selection(project, run)
            chosen = selection.candidate_ids
            return record(
                state,
                "root_selection",
                "done",
                f"模型自动选择 {len(chosen)} 个词根",
                root_selection_id=_candidate_ref(selection),
            )
        payload = human_payload(
            "root_selection",
            "确认词根",
            "在词根面板选择最小商品词根并保存，核验结果仍由核验面板处理。",
            refs={"run": str(run)},
        )
        chosen = await_fact(lambda: gates.root_selection(deps, project, run), payload)
        return record(
            state,
            "root_selection",
            "done",
            "已选词根",
            root_selection_id=_candidate_ref(chosen),
        )

    def week(state: WorkflowState) -> dict[str, object]:
        project, run = ids(state)
        if gates.hot_core_rules(deps, project, run, state):
            # 新任务固定使用最近已结束周；已有报告则恢复它的原周，不能悄悄重采集。
            saved = gates.aba_report(deps, project, run, state["aba_report_id"])
            day = saved.week_end if saved is not None else latest_saturday()
            return record(
                state,
                "week",
                "done",
                f"热门搜索词报告周 {day}",
                aba_week_end=day.isoformat(),
                seller_start=None,
                seller_end=None,
                core_rule_version="hot-search-core-v2",
            )
        selected = state["aba_week_end"] or state["options"].get("report_week_end")
        if selected:
            day = _week_value({"aba_week_end": selected})
            if day is None:
                raise DomainError(422, "WORKFLOW_WEEK_INVALID", "请选择已结束报告周的周六")
            return record(
                state,
                "week",
                "done",
                f"已选周 {day}",
                core_rule_version="core-terms-v1",
                aba_week_end=day.isoformat(),
                seller_start=None,
                seller_end=None,
            )
        if auto_decide(state):
            day = latest_saturday()
            return record(
                state,
                "week",
                "done",
                f"自动选择报告周 {day}",
                core_rule_version="core-terms-v1",
                aba_week_end=day.isoformat(),
                seller_start=None,
                seller_end=None,
            )
        payload = human_payload(
            "week",
            "选择 ABA 报告周",
            "提交 ABA 已结束周的周六（YYYY-MM-DD）；卖家精灵使用独立的最近30天窗口。",
            refs={"run": state["run_id"]},
        )
        while True:
            week_end = _week_value(interrupt(payload))
            if week_end is not None:
                break
            payload = human_payload(
                "week",
                "选择 ABA 报告周",
                "日期无效：必须是已结束周的周六（YYYY-MM-DD）。",
                refs={"run": state["run_id"]},
            )
        return record(
            state,
            "week",
            "done",
            f"报告周 {week_end}",
            core_rule_version="core-terms-v1",
            aba_week_end=week_end.isoformat(),
            seller_start=None,
            seller_end=None,
        )

    def collect_hot_search_report(state: WorkflowState) -> dict[str, object]:
        project, run = ids(state)
        fields: dict[str, object] = {"core_rule_version": "hot-search-core-v2"}
        try:
            report_id = state["aba_report_id"]
            analysis_id = state.get("hot_search_core_analysis_id")
            analysis = (
                deps.hot_search_core_analysis.get(project, run, UUID(analysis_id))
                if analysis_id
                else None
            )
            if not report_id and analysis is not None:
                report_id = str(analysis.report_id)
            current = gates.core_selection(deps, project, run)
            if not report_id and current is not None and not state["options"].get("reanalyze_core"):
                report_id = str(current.report_id)
            if not report_id and not state["options"].get("reanalyze_core"):
                # Existing v1 runs already own a valid report. Restoring them must not create a
                # second report merely because new runs now use the v2 streaming collector.
                for historical in deps.aba.history(project, run, 0, 20):
                    if historical.query_mode == "product_term" and not historical.stale:
                        report_id = str(historical.id)
                        break
            if report_id:
                # A saved pointer is an instruction to restore, never to create another report.
                report = deps.aba.get(project, run, UUID(report_id))
                if report.stale:
                    raise DomainError(
                        409, "HOT_SEARCH_REPORT_STALE", "已保存周报已过期，请检查商品词和资料"
                    )
            else:
                job_id = state.get("hot_search_collection_id")
                job = (
                    deps.hot_search_collection.get(project, run, UUID(job_id))
                    if job_id
                    else deps.hot_search_collection.start(project, run)
                )
                fields["hot_search_collection_id"] = str(job.id)
                if job.status in ("queued", "collecting"):
                    job = deps.hot_search_collection.execute(project, run, job.id)
                if job.status != "completed" or job.report_id is None:
                    failed = next((task for task in job.tasks if task.status == "failed"), None)
                    raise DomainError(
                        409,
                        failed.error_code if failed else "HOT_SEARCH_COLLECTION_RUNNING",
                        failed.error if failed else "热门搜索词候选仍在采集",
                    )
                report = deps.aba.get(project, run, job.report_id)
            fields.update(aba_report_id=str(report.id), aba_week_end=report.week_end.isoformat())
            if (
                not analysis_id
                and current is not None
                and current.rule_version in ("hot-search-core-v1", "hot-search-core-v2")
                and current.report_id == report.id
                and not state["options"].get("reanalyze_core")
            ):
                analysis_id = str(current.source_analysis_id or current.analysis_id)
            if analysis_id:
                if analysis is None:
                    analysis = deps.hot_search_core_analysis.get(project, run, UUID(analysis_id))
            elif auto_decide(state) or state["options"].get("reanalyze_core"):
                # Explicit reanalysis is an execution request even in manual-review mode.
                # The analysis result still pauses at the existing core-term review gate.
                analysis = deps.hot_search_core_analysis.start(
                    project, run, report.id, own_brands(state),
                    retry_incomplete=bool(state["options"].get("reanalyze_core")),
                    force_reanalysis=bool(state["options"].get("reanalyze_core")),
                )
            if analysis is not None:
                fields.update(
                    hot_search_core_analysis_id=str(analysis.id),
                    hot_search_core_analysis_status=analysis.status,
                )
        except DomainError as error:
            return {**failure(state, "collect_core_report", error), **fields}
        return record(
            state,
            "collect_core_report",
            "skipped" if report_id else "done",
            f"{len(report.reports)} 个商品词查询；已恢复周报"
            if report_id
            else f"{len(report.reports)} 个商品词查询；已创建热门搜索词分析",
            **fields,
        )

    def collect_core_report(state: WorkflowState) -> dict[str, object]:
        project, run = ids(state)
        if gates.hot_core_rules(deps, project, run, state):
            return collect_hot_search_report(state)
        if state["options"].get("reanalyze_core") and not state["aba_report_id"]:
            saved_core = gates.core_selection(deps, project, run)
            if saved_core is not None and not saved_core.stale:
                saved_report = deps.aba.get(project, run, saved_core.report_id)
                if not saved_report.stale and saved_report.week_end == week_end(state):
                    return record(
                        state,
                        "collect_core_report",
                        "skipped",
                        "复用已确认核心词的原周报",
                        core_rule_version="core-terms-v1",
                        aba_report_id=str(saved_report.id),
                    )
        existing = gates.aba_report(deps, project, run, state["aba_report_id"])
        if existing is not None and not existing.stale:
            return record(
                state,
                "collect_core_report",
                "skipped",
                "已有有效周报",
                core_rule_version="core-terms-v1",
                aba_report_id=str(existing.id),
            )
        try:
            report = deps.aba.collect(
                project,
                run,
                AbaRequest(week_end=week_end(state), query_mode="root", max_rows=1000),
            )
        except DomainError as error:
            return failure(state, "collect_core_report", error)
        return record(
            state,
            "collect_core_report",
            "done",
            f"{len(report.reports)} 个查询",
            core_rule_version="core-terms-v1",
            aba_report_id=str(report.id),
        )

    def hot_core_terms(state: WorkflowState) -> dict[str, object]:
        project, run = ids(state)
        fields: dict[str, object] = {"core_rule_version": "hot-search-core-v2"}
        try:
            # collect_core_report 已在 reanalyze_core 下创建新的分析 ID；这里要执行它。
            analysis_id = state.get("hot_search_core_analysis_id")
            if not analysis_id:
                raise DomainError(
                    409, "HOT_SEARCH_ANALYSIS_REQUIRED", "请先采集热门搜索词并创建分析"
                )
            analysis = deps.hot_search_core_analysis.get(project, run, UUID(analysis_id))
            if analysis.status == "queued":
                analysis = deps.hot_search_core_analysis.execute(project, run, analysis.id)
            fields.update(
                hot_search_core_analysis_id=str(analysis.id),
                hot_search_core_analysis_status=analysis.status,
                core_analysis_id=str(analysis.id),
            )
            if analysis.status not in ("completed", "incomplete"):
                reasons = {
                    "running": "热门搜索词分析仍在进行；恢复时不会重复发起付费请求",
                    "failed": "热门搜索词分析失败，未保存核心词，请检查分析证据",
                    "stale": "热门搜索词分析已过期，请检查商品词和资料",
                }
                raise DomainError(
                    409,
                    "HOT_SEARCH_ANALYSIS_" + analysis.status.upper(),
                    reasons.get(analysis.status, "热门搜索词分析尚未完成"),
                )
            chosen = analysis.recommended_candidate_ids
            if not chosen:
                if analysis.items:

                    def current_manual_selection() -> CoreSelection | None:
                        saved = gates.core_selection(deps, project, run)
                        if (
                            saved is not None
                            and saved.rule_version in ("hot-search-core-v1", "hot-search-core-v2")
                            and saved.analysis_id == analysis.id
                        ):
                            return saved
                        return None

                    selection = await_fact(
                        current_manual_selection,
                        human_payload(
                            "core_terms",
                            "人工筛选核心大词",
                            f"自动规则未选出合格核心词；请在核心大词面板从 "
                            f"{len(analysis.items)} 个 ABA 排名 25 万以内候选中人工勾选并保存。",
                            refs={
                                "analysis_id": str(analysis.id),
                                "candidate_count": str(len(analysis.items)),
                            },
                        ),
                    )
                    fields["core_selection_id"] = str(selection.id)
                    return record(
                        state,
                        "core_terms",
                        "done",
                        f"自动无合格项；人工确认 {len(selection.items)} 个核心词",
                        **fields,
                    )
                excluded = sum(item.status == "failed" for item in analysis.items)
                raise DomainError(
                    409,
                    "HOT_SEARCH_CORE_EMPTY",
                    f"分析{'不完整' if analysis.status == 'incomplete' else '完成'}："
                    f"已检查 {len(analysis.items)} 个热门搜索词，{excluded} 个失败，"
                    "ABA 排名 25 万以内没有可供人工筛选的候选词",
                )
            current = gates.core_selection(deps, project, run)
            selection = deps.core.select(
                project,
                run,
                CoreSelectionInput(
                    analysis_id=analysis.id,
                    rule_version="hot-search-core-v2",
                    candidate_ids=chosen,
                    expected_version=current.version if current else 0,
                ),
            )
            fields["core_selection_id"] = str(selection.id)
        except DomainError as error:
            return {**failure(state, "core_terms", error), **fields}
        qualifier = (
            "分析不完整；已保存成功合格项" if analysis.status == "incomplete" else "分析完成"
        )
        return record(
            state,
            "core_terms",
            "done",
            f"{qualifier}，自动确认全部 {len(chosen)} 个合格核心词",
            **fields,
        )

    def core_terms(state: WorkflowState) -> dict[str, object]:
        project, run = ids(state)
        hot_rules = gates.hot_core_rules(deps, project, run, state)
        current = gates.core_selection(deps, project, run)
        if state["core_selection_id"] and (
            current is None or str(current.id) != state["core_selection_id"]
        ):
            return failure(
                state,
                "core_terms",
                DomainError(
                    409,
                    "WORKFLOW_CORE_SELECTION_MISSING",
                    "检查点中的已确认核心词选择不存在或已被替换",
                ),
            )
        reanalyze = bool(state["options"].get("reanalyze_core")) and not state["core_selection_id"]
        if current is not None and not reanalyze:
            if hot_rules:
                analysis = deps.hot_search_core_analysis.get(project, run, current.analysis_id)
                detail = "已确认全部合格核心词"
                if analysis.status == "incomplete":
                    detail += "；分析不完整"
                return record(
                    state,
                    "core_terms",
                    "skipped",
                    detail,
                    core_selection_id=str(current.id),
                    core_analysis_id=str(current.analysis_id),
                    hot_search_core_analysis_id=str(analysis.id),
                    hot_search_core_analysis_status=analysis.status,
                    core_rule_version=current.rule_version,
                )
            return record(
                state,
                "core_terms",
                "skipped",
                "已确认核心大词",
                core_selection_id=str(current.id),
                core_analysis_id=str(current.analysis_id),
            )
        if auto_decide(state):
            if hot_rules:
                return hot_core_terms(state)
            report_id = state["aba_report_id"]
            if not report_id:
                return failure(
                    state,
                    "core_terms",
                    DomainError(409, "CORE_REPORT_STALE", "尚无 ABA 周报"),
                )
            brands = own_brands(state)
            report_key = UUID(report_id)
            # 详审每批 20 个相关候选，默认最多 50 批；与粗筛读取数量是两个独立上限。
            max_pages = option_int(state, "core_max_pages", 50)

            def analyze_page(offset: int, candidate_ids: list[UUID]) -> CoreAnalysis:
                history = deps.core.history(project, run, report_key)
                if any(
                    item.request.candidate_ids == candidate_ids and item.status == "outcome_pending"
                    for item in history
                ):
                    raise DomainError(
                        502, "CORE_OUTCOME_UNKNOWN", "该页模型结果待确认，不会重复调用"
                    )
                last: DomainError | None = None
                # 仅重试确定收到的无效输出；处理中或结果未知不能另开付费请求。
                for attempt in range(3):
                    try:
                        return deps.core.analyze(
                            project,
                            run,
                            report_key,
                            CoreRequest(
                                offset=offset,
                                limit=20,
                                own_brands=brands,
                                candidate_ids=candidate_ids,
                            ),
                            retry=attempt,
                        )
                    except DomainError as error:
                        last = error
                        if error.code == "MODEL_OUTPUT_INVALID":
                            continue
                        raise
                assert last is not None
                raise last

            try:
                screened = deps.core.prefilter(project, run, report_key)
                offset = 0
                analysed = 0
                batch_ids: list[UUID] = []
                while analysed < max_pages:
                    batch_candidate_ids = screened.candidate_ids[offset : offset + 20]
                    if not batch_candidate_ids:
                        break
                    analysis = analyze_page(offset, batch_candidate_ids)
                    batch_ids.append(analysis.id)
                    analysed += 1
                    offset += 20
                    if offset >= len(screened.candidate_ids):
                        break
                if not batch_ids:
                    raise DomainError(409, "CORE_NO_CANDIDATES", "没有可分析的核心词候选")
                aggregate = deps.core.aggregate(
                    project,
                    run,
                    batch_ids,
                    screened_candidates=screened.screened_candidates,
                    relevant_candidates=len(screened.candidate_ids),
                )
                # 先汇总所有已审核批次再选最多 10 个；合格不足 10 个时保留实际数量。
                # 不提前遇到一个推荐词就终止，也不使用属性长尾凑满核心品类词。
                chosen = [item.id for item in aggregate.items if item.recommended][:10]
                if not chosen:
                    raise DomainError(
                        409,
                        "CORE_NO_CANDIDATES",
                        f"已粗筛 {screened.screened_candidates} 个候选、详审 {len(aggregate.items)} 个相关词，"
                        "未发现合格核心品类词，请检查商品信息、词根及审核结果",
                    )
                selection = deps.core.select(
                    project,
                    run,
                    CoreSelectionInput(
                        analysis_id=aggregate.id,
                        candidate_ids=chosen,
                        expected_version=current.version if current is not None else 0,
                    ),
                )
                return record(
                    state,
                    "core_terms",
                    "done",
                    f"汇总 {analysed} 批后自动确认 {len(chosen)} 个核心词",
                    core_selection_id=str(selection.id),
                    core_analysis_id=str(selection.analysis_id),
                )
            except DomainError as error:
                return failure(state, "core_terms", error)
        payload = human_payload(
            "core_terms",
            "分析并确认核心大词",
            "在核心词面板按页分析并勾选高相关通用词后保存；编排不会自动调用模型。",
            refs={"aba_report": state["aba_report_id"] or ""},
        )
        chosen = await_fact(lambda: gates.core_selection(deps, project, run), payload)
        return record(
            state,
            "core_terms",
            "done",
            f"已确认 {len(chosen.items)} 个核心词",
            core_selection_id=str(chosen.id),
            core_analysis_id=str(chosen.analysis_id),
        )

    return {
        "ensure_input": ensure_input,
        "identify_product": identify_product,
        "product_confirmation": product_confirmation,
        "generate_roots": generate_roots,
        "root_selection": root_selection,
        "week": week,
        "collect_core_report": collect_core_report,
        "core_terms": core_terms,
    }
