"""第 3–5 步节点：竞对发现与确认、ABA 拓词提交/等待、三方词表门。"""

from typing import Literal

from langgraph.types import interrupt

from app.domain.aba_expansion import Direction, ExpansionReport, ExpansionRequest
from app.domain.cleaning import CleaningRequest
from app.domain.competitors import (
    Competitors,
    Review,
    SelectionInput,
)
from app.domain.errors import DomainError
from app.domain.sellersprite import BrowserCollectionInput, BrowserCollectionJob
from app.domain.text import phrase_key
from app.domain.third_party import ImportConfirmation, ImportReport
from app.workflows import gates
from app.workflows.deps import WorkflowDeps
from app.workflows.errors import AGENT_ATTENTION_ERRORS, AGENT_NETWORK_ERRORS, failure
from app.workflows.nodes_common import (
    Node,
    auto_decide,
    await_fact,
    cleaning_request,
    external_payload,
    human_payload,
    ids,
    model_client,
    product_of,
    week_end,
)
from app.workflows.state import WorkflowState, record


def _automatic_competitors_ready(item: Competitors | None) -> bool:
    if item is None or item.stale or item.confirmed_at is None or not item.poe_downloads:
        return False
    included = [candidate for candidate in item.items if candidate.decision == "include"]
    return bool(included) and all(entry.status == "completed" for entry in item.poe_downloads) and all(
        candidate.price_basis == "average_30d" and candidate.price_status == "within"
        and candidate.relevance_score is not None and candidate.relevance_score > 40
        for candidate in included
    )


def _confirmed_mapping(
    report: ImportReport,
) -> tuple[dict[str, str], Literal["fraction", "percent"]]:
    """自动确认字段映射；份额单位按样例行是否带百分号判断。"""
    mapping = dict(report.suggested_mapping)
    if "keyword" not in mapping:
        raise DomainError(422, "IMPORT_MAPPING_REQUIRED", "词表缺少关键词列，无法自动确认")
    share_columns = [column for field, column in mapping.items() if field.endswith("_share")]
    percent = any(
        "%" in sample.get(column, "") for sample in report.samples for column in share_columns
    )
    return mapping, "percent" if percent else "fraction"


def build_nodes_3_5(deps: WorkflowDeps) -> dict[str, Node]:
    def competitor_discovery(state: WorkflowState) -> dict[str, object]:
        project, run = ids(state)
        existing = gates.competitors(deps, project, run)
        if auto_decide(state) and _automatic_competitors_ready(existing):
            return record(state, "competitor_discovery", "skipped", "已有真实 30 天价格及四维校验通过的竞对")
        if not auto_decide(state) and existing is not None and existing.items and not existing.stale:
            return record(state, "competitor_discovery", "skipped", "已有竞对候选")
        if auto_decide(state):
            try:
                core = gates.core_selection(deps, project, run)
                if core is None:
                    raise DomainError(409, "CORE_SELECTION_REQUIRED", "请先确认核心词")
                expected = existing.version if existing is not None else 0
                found = deps.competitors.download_poe_all(project, run, expected)
                failures = [entry for entry in found.poe_downloads if entry.status == "failed"]
                if failures:
                    raise DomainError(422, "POE_BATCH_INCOMPLETE", "；".join(f"{entry.keyword}：{entry.error_code}" for entry in failures))
                if not _automatic_competitors_ready(found):
                    raise DomainError(422, "COMPETITOR_30_DAY_QUALIFIED_REQUIRED", "尚无同时满足真实 30 天均价 ±15% 与四维相关度 >40 的竞对")
            except DomainError as error:
                return failure(state, "competitor_discovery", error)
            return record(
                state,
                "competitor_discovery",
                "done",
                f"自动发现 {len(found.items)} 个候选",
            )
        payload = human_payload(
            "competitor_discovery",
            "发现竞对 ASIN",
            "在竞对面板用人工录入、前台搜索、商机探测器 CSV 或商品详情添加候选。",
            refs={"core_selection": state["core_selection_id"] or ""},
        )

        def discovered() -> Competitors | None:
            item = gates.competitors(deps, project, run)
            return item if item is not None and item.items and not item.stale else None

        await_fact(discovered, payload)
        return record(state, "competitor_discovery", "done", "已有竞对候选")

    def competitor_selection(state: WorkflowState) -> dict[str, object]:
        project, run = ids(state)
        current = gates.competitors(deps, project, run)
        if auto_decide(state) and not _automatic_competitors_ready(current):
            return failure(state, "competitor_selection", DomainError(
                422, "COMPETITOR_30_DAY_QUALIFIED_REQUIRED", "自动竞对须先完成真实 30 天均价和四维筛选"
            ))
        if current is not None and current.confirmed_at is not None and not current.stale:
            return record(
                state,
                "competitor_selection",
                "skipped",
                "已确认竞对",
                competitors_id=str(current.id),
            )

        if auto_decide(state):
            candidates = gates.competitors(deps, project, run)
            if candidates is None:
                return failure(
                    state,
                    "competitor_selection",
                    DomainError(409, "COMPETITORS_REQUIRED", "尚无竞对候选"),
                )
            if candidates.stale:
                return failure(
                    state,
                    "competitor_selection",
                    DomainError(
                        409,
                        "COMPETITORS_STALE",
                        "候选已过期，先按当前核心大词重新发现竞对",
                    ),
                )
            try:
                decision = model_client(deps).review_competitors(
                    product_of(deps, project, run), candidates.items
                )
                judgments = {item.asin: item for item in decision.items}
                if set(judgments) != {item.asin for item in candidates.items}:
                    raise DomainError(502, "MODEL_OUTPUT_INVALID", "模型未覆盖全部竞对候选")
                reviews = [
                    Review(
                        asin=item.asin,
                        decision=judgments[item.asin].decision,
                        reason=judgments[item.asin].reason,
                        price_override_reason=judgments[item.asin].price_override_reason,
                    )
                    for item in candidates.items
                ]
                chosen = deps.competitors.select(
                    project,
                    run,
                    SelectionInput(
                        expected_version=candidates.version,
                        reference_price=candidates.reference_price,
                        currency=candidates.currency,
                        tolerance=candidates.tolerance,
                        reviews=reviews,
                    ),
                )
            except DomainError as error:
                return failure(state, "competitor_selection", error)
            retained = sum(1 for item in chosen.items if item.decision == "include")
            return record(
                state,
                "competitor_selection",
                "done",
                f"模型保留 {retained} 个竞对",
                competitors_id=str(chosen.id),
            )

        def confirmed() -> Competitors | None:
            item = gates.competitors(deps, project, run)
            if item is None or item.confirmed_at is None or item.stale:
                return None
            return item

        payload = human_payload(
            "competitor_selection",
            "确认竞对",
            "逐项选择纳入或排除、填写理由并保存；价格越界时还需填写价格核对说明。",
            refs={"core_selection": state["core_selection_id"] or ""},
        )
        chosen = await_fact(confirmed, payload)
        retained = sum(1 for item in chosen.items if item.decision == "include")
        return record(
            state,
            "competitor_selection",
            "done",
            f"保留 {retained} 个竞对",
            competitors_id=str(chosen.id),
        )

    # 复用正在执行或已成功且未失效的报告；partial/failed 不能当作完整采集通过门控。
    def _reusable(report: ExpansionReport | None) -> bool:
        return report is not None and not report.stale and report.status in ("running", "succeeded")

    def submit_sources(state: WorkflowState) -> dict[str, object]:
        project, run = ids(state)
        day = week_end(state)
        try:
            forward = gates.expansion(deps, project, run, state["expansion_forward_id"])
            if not _reusable(forward):
                forward = gates.latest_expansion(deps, project, run, "forward", day)
            if not _reusable(forward):
                forward = deps.expansion.collect(
                    project,
                    run,
                    ExpansionRequest(direction="forward", week_end=day, max_rows=1000),
                )
            reverse = gates.expansion(deps, project, run, state["expansion_reverse_id"])
            if not _reusable(reverse):
                reverse = gates.latest_expansion(deps, project, run, "reverse", day)
            if not _reusable(reverse):
                reverse = deps.expansion.collect(
                    project,
                    run,
                    ExpansionRequest(direction="reverse", week_end=day, max_rows=1000),
                )
        except DomainError as error:
            return failure(state, "submit_sources", error)
        # 两条分支都会得到报告：复用已有非空报告，或 collect 返回新报告。
        assert forward is not None and reverse is not None
        return record(
            state,
            "submit_sources",
            "done",
            "已提交正反向采集",
            expansion_forward_id=str(forward.id),
            expansion_reverse_id=str(reverse.id),
        )

    def wait_sources(state: WorkflowState) -> dict[str, object]:
        project, run = ids(state)
        day = week_end(state)

        def latest(direction: Direction) -> ExpansionReport | None:
            return gates.latest_expansion(deps, project, run, direction, day)

        while True:
            forward, reverse = latest("forward"), latest("reverse")
            reports = [item for item in (forward, reverse) if item is not None]
            if (
                forward is not None
                and reverse is not None
                and forward.status == "succeeded"
                and reverse.status == "succeeded"
            ):
                return record(
                    state,
                    "wait_sources",
                    "done",
                    "正反向均采集成功",
                    expansion_forward_id=str(forward.id),
                    expansion_reverse_id=str(reverse.id),
                )
            # 提交成功只表示任务已创建；正反向都成功后才能进入下游。
            # 暂停期间由页面轮询推动恢复，恢复时重新查询最新报告以支持面板手动重试。
            blocked = any(item.status in ("failed", "partial", "interrupted") for item in reports)
            remote_error = next(
                (
                    query
                    for report in reports
                    for query in report.queries
                    if query.error_code in AGENT_NETWORK_ERRORS | AGENT_ATTENTION_ERRORS
                ),
                None,
            )
            if remote_error is not None and remote_error.error_code is not None:
                return failure(
                    state,
                    "wait_sources",
                    DomainError(
                        503,
                        remote_error.error_code,
                        remote_error.error or "ABA 采集端需要处理",
                    ),
                )
            interrupt(
                external_payload(
                    "sources",
                    "等待 ABA 拓词",
                    "采集在后台执行；失败或截断时可在拓词面板重试，编排会读取最新一次结果。"
                    if blocked
                    else "采集在后台执行，完成后自动继续。",
                    refs={
                        "forward": str(forward.id) if forward else "",
                        "reverse": str(reverse.id) if reverse else "",
                        "background_poll": "1",
                    },
                    # 由后端调度器轮询完成状态；前端只读展示。
                    ready=True,
                )
            )

    def third_party(state: WorkflowState) -> dict[str, object]:
        project, run = ids(state)
        request: CleaningRequest = cleaning_request(state)

        def complete_sources():
            # 只要求四路都存在；截断的来源按已获取部分使用，缺的部分不再补。
            return gates.cleaning_sources(deps, project, run, request)

        payload = human_payload(
            "third_party",
            "补齐三方词表",
            "每个核心词与每个保留竞对 ASIN 都需要已确认的卖家精灵词表；"
            "采集结果截断时按已获取部分继续，缺的部分不再补齐。",
            refs={"week": state["aba_week_end"] or ""},
        )

        def sources_message(items) -> str:
            truncated = sum(1 for item in items if item.truncated)
            if not truncated:
                return "四路来源齐全"
            return f"四路来源齐全（{truncated} 路采集截断，按已获取部分继续）"

        sources = gates.cleaning_sources(deps, project, run, request)
        if sources is not None:
            return record(
                state,
                "third_party",
                "skipped",
                sources_message(sources),
                third_party_ids=[
                    str(item.id) for item in sources if item.provider == "sellersprite"
                ],
            )
        if not auto_decide(state):
            sources = await_fact(complete_sources, payload)
            return record(
                state,
                "third_party",
                "done",
                sources_message(sources),
                third_party_ids=[
                    str(item.id) for item in sources if item.provider == "sellersprite"
                ],
            )

        period = "last_30_days"
        while True:
            try:
                jobs = deps.sellersprite.list(project, run)
                contexts = {
                    direction: deps.expansion.context(project, run, direction)[:2]
                    for direction in ("forward", "reverse")
                }
                jobs = [
                    job
                    for job in jobs
                    if isinstance(job.request, BrowserCollectionInput)
                    and job.request.period == period
                    and (job.core_selection_id, job.competitor_id)
                    == contexts[job.request.direction]
                ]
                for job in jobs:
                    if (
                        job.status != "succeeded"
                        or job.report_id is None
                        or not isinstance(job.request, BrowserCollectionInput)
                        or job.request.period != period
                    ):
                        continue
                    report = deps.third_party.get(project, run, job.report_id)
                    if report.stale:
                        continue
                    if report.status == "preview":
                        mapping, unit = _confirmed_mapping(report)
                        deps.third_party.confirm(
                            project,
                            run,
                            report.id,
                            ImportConfirmation(mapping=mapping, share_unit=unit),
                        )
                    elif not (report.complete or report.truncated):
                        raise DomainError(
                            409,
                            "THIRD_PARTY_INCOMPLETE",
                            f"{report.filename} 导出不完整，请人工处理",
                        )
                missing = deps.third_party.missing_queries(
                    project, run, request.seller_start, request.seller_end, request.seller_period
                )
                if not missing:
                    sources = gates.cleaning_sources(deps, project, run, request)
                    if sources is not None:
                        if any(source.truncated for source in sources):
                            sources = await_fact(complete_sources, payload)
                        return record(
                            state,
                            "third_party",
                            "done",
                            "四路来源齐全",
                            third_party_ids=[
                                str(item.id) for item in sources if item.provider == "sellersprite"
                            ],
                        )
                    raise DomainError(409, "THIRD_PARTY_INCOMPLETE", "三方词表仍不完整")
                latest_jobs: dict[tuple[str, str], BrowserCollectionJob] = {}
                failed_count: dict[tuple[str, str], int] = {}
                # jobs 按时间从新到旧；成功后不再累计更早的失败，否则一次成功也洗不掉
                # 历史失败记录，后续偶发失败会被误判为“连续失败 3 次”而直接交人工。
                recovered: set[tuple[str, str]] = set()
                for job in jobs:
                    if (
                        not isinstance(job.request, BrowserCollectionInput)
                        or job.request.period != period
                    ):
                        continue
                    for source_query in job.request.queries:
                        key = (job.request.direction, phrase_key(source_query))
                        latest_jobs.setdefault(key, job)
                        if job.status == "failed":
                            if key not in recovered:
                                failed_count[key] = failed_count.get(key, 0) + 1
                        else:
                            recovered.add(key)
                for direction, query in missing:
                    key = (direction, phrase_key(query))
                    job = latest_jobs.get(key)
                    if (
                        job is not None
                        and job.status == "failed"
                        and job.error_code in AGENT_NETWORK_ERRORS | AGENT_ATTENTION_ERRORS
                    ):
                        raise DomainError(
                            503, job.error_code, job.error or "卖家精灵采集端需要处理"
                        )
                for direction, query in missing:
                    if direction == "reverse":
                        continue
                    key = (direction, phrase_key(query))
                    job = latest_jobs.get(key)
                    if job is not None and job.status == "failed":
                        # 同一查询连续失败 3 次才交人工：留出重跑余量，又不会无界消耗导出次数。
                        if failed_count.get(key, 0) >= 3:
                            raise DomainError(
                                502,
                                "THIRD_PARTY_COLLECTION_FAILED",
                                job.error or "卖家精灵采集失败",
                            )
                        deps.sellersprite.create(
                            project,
                            run,
                            BrowserCollectionInput(direction=direction, query=query),
                        )
                        continue
                    if job is None or job.status not in ("queued", "running"):
                        deps.sellersprite.create(
                            project,
                            run,
                            BrowserCollectionInput(direction=direction, query=query),
                        )
                reverse_missing = [query for direction, query in missing if direction == "reverse"]
                for start in range(0, len(reverse_missing), 20):
                    batch = reverse_missing[start : start + 20]
                    batch_keys = [("reverse", phrase_key(query)) for query in batch]
                    active = next(
                        (
                            job
                            for job in jobs
                            if isinstance(job.request, BrowserCollectionInput)
                            and job.request.method == "traffic_extension"
                            and {phrase_key(value) for value in job.request.queries}
                            == {phrase_key(value) for value in batch}
                            and job.status in ("queued", "running")
                        ),
                        None,
                    )
                    if active is not None:
                        continue
                    failed = [
                        latest_jobs[key]
                        for key in batch_keys
                        if key in latest_jobs and latest_jobs[key].status == "failed"
                    ]
                    if failed and all(failed_count.get(key, 0) >= 3 for key in batch_keys):
                        raise DomainError(
                            502,
                            "THIRD_PARTY_COLLECTION_FAILED",
                            failed[0].error or "卖家精灵多 ASIN 拓展失败",
                        )
                    deps.sellersprite.create(
                        project,
                        run,
                        BrowserCollectionInput(
                            direction="reverse",
                            method="traffic_extension",
                            queries=batch,
                            variant_scope="all",
                        ),
                    )
            except DomainError as error:
                return failure(state, "third_party", error)
            stages = []
            for job in jobs:
                stage = str(getattr(job, "stage") or "").strip()
                if not stage:
                    continue
                method = str(getattr(getattr(job, "request", None), "method", "") or "")
                label = {"keyword_mining": "关键词挖掘", "traffic_extension": "流量拓展"}.get(method, method or "采集")
                stages.append(f"{label}：{stage}")
            detail = "浏览器正在逐词采集并自动导入；完成后自动确认字段映射。"
            if stages:
                detail = "；".join(stages[-3:]) + "。" + detail
            interrupt(
                external_payload(
                    "third_party",
                    "等待卖家精灵采集",
                    detail,
                    refs={"period": period, "background_poll": "1"},
                    ready=True,
                )
            )

    return {
        "competitor_discovery": competitor_discovery,
        "competitor_selection": competitor_selection,
        "submit_sources": submit_sources,
        "wait_sources": wait_sources,
        "third_party": third_party,
    }
