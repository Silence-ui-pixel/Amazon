"""第 6–8 步节点：清洗、流量筛选、分层发布。"""

import os
import time
from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from app.domain.cleaning import CleaningReport
from app.domain.decisions import TrafficJudgment
from app.domain.errors import DomainError
from app.domain.library import LibraryReport, LibraryRequest
from app.domain.product_understanding import ProductUnderstanding
from app.domain.traffic import RuleSet, TrafficReport, TrafficRequest, TrafficReview, TrafficRow
from app.services.qwen import QwenClient
from app.workflows import gates
from app.workflows.deps import WorkflowDeps
from app.workflows.errors import failure
from app.workflows.nodes_common import (
    Node,
    auto_decide,
    await_fact,
    cleaning_request,
    human_payload,
    ids,
    model_client,
    product_of,
)
from app.workflows.state import WorkflowState, record

# 10 词批次在实测中 8 路并发仍能 100% 完整返回；小批也便于超时恢复。
TRAFFIC_BATCH = 10
TRAFFIC_MODEL_CONCURRENCY_ENV = "KEYWORD_TRAFFIC_MODEL_CONCURRENCY"


def _chunks[Value](items: list[Value], size: int) -> list[list[Value]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def _traffic_model_concurrency() -> int:
    """限制模型并发为 1～8；配置错误时使用安全默认值 2。"""
    raw = os.getenv(TRAFFIC_MODEL_CONCURRENCY_ENV, "2").strip()
    try:
        configured = int(raw)
    except ValueError:
        configured = 2
    return max(1, min(configured, 8))


def _review_traffic_batch(
    client: QwenClient,
    product: ProductUnderstanding,
    batch: list[TrafficRow],
) -> dict[str, TrafficJudgment]:
    """审核并严格校验单批关键词；只对可纠正的模型格式问题做有界重试。"""
    expected = [item.phrase.keyword for item in batch]
    wanted = set(expected)
    for attempt in range(3):
        try:
            decision = client.review_traffic(product, [_row_payload(item) for item in batch])
        except DomainError as error:
            if error.code == "MODEL_OUTPUT_INVALID":
                if attempt < 2:
                    continue
                # Mimo 偶尔漏掉单个关键词；拆小后继续，不让整轮审核失败。
                if len(batch) > 1:
                    midpoint = len(batch) // 2
                    left = _review_traffic_batch(client, product, batch[:midpoint])
                    right = _review_traffic_batch(client, product, batch[midpoint:])
                    return {**left, **right}
            raise
        returned = [item.keyword for item in decision.items]
        if len(returned) == len(expected) and len(set(returned)) == len(returned):
            covered = {item.keyword: item for item in decision.items}
            if set(covered) == wanted:
                return covered
        # 只在一对一无歧义时接受等价标签；原始关键词仍作为持久化键。
        originals = {_normalize_keyword(keyword): keyword for keyword in expected}
        normalized = {_normalize_keyword(item.keyword): item for item in decision.items}
        if len(returned) == len(expected) == len(originals) == len(normalized) and set(
            normalized
        ) == set(originals):
            return {originals[key]: judgment for key, judgment in normalized.items()}
    # 三次都未完整覆盖：歧义批次拆至单词，仍逐项严格校验，不补造判断。
    if len(batch) > 1:
        midpoint = len(batch) // 2
        left = _review_traffic_batch(client, product, batch[:midpoint])
        right = _review_traffic_batch(client, product, batch[midpoint:])
        return {**left, **right}
    raise DomainError(502, "MODEL_OUTPUT_INVALID", "模型未覆盖本批全部关键词")


def _normalize_keyword(value: str) -> str:
    # U+FFFC 是来源页面的对象占位符，模型常省略它；仅用于标签比较。
    return " ".join(value.replace("\ufffc", "").split()).casefold()


def _match_judgment(judgments: dict[str, TrafficJudgment], keyword: str) -> TrafficJudgment | None:
    direct = judgments.get(keyword)
    if direct is not None:
        return direct
    target = _normalize_keyword(keyword)
    for candidate, judgment in judgments.items():
        if _normalize_keyword(candidate) == target:
            return judgment
    return None


def _row_payload(row: TrafficRow) -> dict[str, object]:
    metrics = row.metrics
    return {
        "keyword": row.phrase.keyword,
        "rank": metrics.rank,
        "purchase": metrics.purchase,
        "monopoly": metrics.monopoly,
        "purchase_ratio": (
            str(metrics.purchase_ratio) if metrics.purchase_ratio is not None else None
        ),
        "missing": list(metrics.missing),
        "source_metrics": [
            {
                "provider": item.provider,
                "direction": item.direction,
                "rank": item.metrics.rank,
                "purchase_ratio": (
                    str(item.metrics.purchase_ratio)
                    if item.metrics.purchase_ratio is not None
                    else None
                ),
                "purchase": item.metrics.purchase,
                "monopoly": item.metrics.monopoly,
                "missing": list(item.metrics.missing),
            }
            for item in row.source_metrics
        ],
    }


def _automatic_priority(
    row: TrafficRow,
    decision: Literal["keep", "exclude", "defer"],
    rules: RuleSet | None = None,
) -> Literal["high", "normal", "low"]:
    """投放优先级只由已计算指标决定，不接受模型主观改写。"""
    selected = rules or RuleSet()
    metrics = row.metrics
    if decision != "keep":
        return "low"
    if (
        metrics.rank is None
        or metrics.purchase is None
        or metrics.monopoly is None
        or metrics.purchase is False
        or metrics.monopoly is True
    ):
        return "low"
    return "high" if metrics.rank <= selected.deep_rank_max else "normal"


def review_traffic_report(
    deps: WorkflowDeps, project: UUID, run: UUID, report: TrafficReport
) -> tuple[int, int]:
    """面板与自动编排共用审核规则；每个完成批次立即持久化，重试只处理剩余词。"""
    # 同一个报告的面板任务和自动编排共用数据库互斥锁，避免交叉启动重复调用模型。
    with deps.repository.connect() as claim:
        claim.execute(
            "SELECT pg_advisory_xact_lock(hashtext(?))", ("traffic-model-review:" + str(report.id),)
        )
        latest = deps.traffic.get(project, run, report.id)
        if latest.stale:
            raise DomainError(409, "TRAFFIC_INPUT_STALE", "流量筛选快照已失效，请重新生成")
        if latest.complete:
            return latest.version, 0
        return _review_traffic_report(deps, project, run, latest)


def _review_traffic_report(
    deps: WorkflowDeps, project: UUID, run: UUID, report: TrafficReport
) -> tuple[int, int]:
    rows = deps.traffic.page(project, run, report.id, 0, report.total).items
    pending = [
        item
        for item in rows
        if not item.cleaning_exclusion
        and (item.review is None or item.review.data.decision == "defer")
    ]
    version = report.version
    product = product_of(deps, project, run)
    client = model_client(deps)
    concurrency = _traffic_model_concurrency()
    batches = _chunks(pending, TRAFFIC_BATCH)
    # 滚动提交：始终维持 concurrency 个在途请求，每批完成立即写库，
    # 慢批次不再拖住同窗口的其他结果。
    executor = ThreadPoolExecutor(max_workers=concurrency)
    try:
        futures = {
            executor.submit(_review_traffic_batch, client, product, batch): batch
            for batch in batches
        }
        first_error: DomainError | None = None
        for future in as_completed(futures):
            batch = futures[future]
            try:
                judgments = future.result()
            except CancelledError:
                continue
            except DomainError as error:
                if first_error is None:
                    first_error = error
                    for queued in futures:
                        queued.cancel()
                continue
            reviews: list[TrafficReview] = []
            for item in batch:
                # 防御：模型可能返回等价但大小写/空格略异的 keyword。
                judgment = _match_judgment(judgments, item.phrase.keyword)
                if judgment is None:
                    raise DomainError(
                        502,
                        "MODEL_OUTPUT_INVALID",
                        "模型返回的关键词与请求不一致",
                    )
                # 自动编排没有补数据步骤；defer 落到“低优先级保留”，避免反复重审。
                if judgment.decision == "defer":
                    final_decision: Literal["keep", "exclude", "defer"] = "keep"
                    final_priority: Literal["high", "normal", "low"] = "low"
                    final_reason = f"{judgment.reason}（自动模式指标不足，保留待人工核对）"
                else:
                    final_decision = judgment.decision
                    final_priority = _automatic_priority(item, judgment.decision, report.rules)
                    final_reason = judgment.reason
                reviews.append(
                    TrafficReview(
                        expected_version=version,
                        keyword=item.phrase.keyword,
                        decision=final_decision,
                        priority=final_priority,
                        reason=final_reason,
                        evidence_note=judgment.evidence_note,
                        reviewer="自动编排",
                    ),
                )
            updated = deps.traffic.review_automatic_batch(project, run, report.id, reviews)
            version = updated.version
        if first_error is not None:
            raise first_error
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    return version, len(pending)


def build_nodes_6_8(deps: WorkflowDeps) -> dict[str, Node]:
    def create_cleaning(state: WorkflowState) -> dict[str, object]:
        project, run = ids(state)
        existing = gates.cleaning(
            deps, project, run, state["cleaning_id"], state["cleaning_version"]
        )
        if existing is not None and not existing.stale:
            return record(
                state,
                "create_cleaning",
                "skipped",
                "已有清洗快照",
                cleaning_id=str(existing.id),
                cleaning_version=existing.version,
            )
        try:
            report = deps.cleaning.create(project, run, cleaning_request(state))
        except DomainError as error:
            return failure(state, "create_cleaning", error)
        return record(
            state,
            "create_cleaning",
            "done",
            f"{report.phrase_count} 词 / {report.frequency_count} 词频",
            cleaning_id=str(report.id),
            cleaning_version=report.version,
        )

    def cleaned(state: WorkflowState) -> CleaningReport | None:
        project, run = ids(state)
        report = gates.cleaning_any(deps, project, run, state["cleaning_id"])
        if report is None or report.review is None or report.stale:
            return None
        # 部分失败/执行中/结果未知都不算完成；否则下游会把未分类词当成已定类。
        progress = deps.cleaning.classification_progress(project, run, report.id)
        if progress.get("status") in ("running", "failed", "outcome_pending", "not_started"):
            return None
        return report

    def cleaning_review(state: WorkflowState) -> dict[str, object]:
        current = cleaned(state)
        if current is not None:
            return record(
                state,
                "cleaning_review",
                "skipped",
                "已审核清洗",
                cleaning_version=current.version,
            )
        project, run = ids(state)
        report = gates.cleaning_any(deps, project, run, state["cleaning_id"])
        if report is None:
            return failure(
                state,
                "cleaning_review",
                DomainError(409, "WORKFLOW_CLEANING_MISSING", "尚无清洗快照"),
            )
        retry = state["options"].get("retry_cleaning") is True
        if not retry:
            # 自动编排：上次分类失败时接着补跑，不依赖人工点击。
            progress = deps.cleaning.classification_progress(project, run, report.id)
            retry = progress.get("status") == "failed"
        try:
            updated = deps.cleaning.classify(
                project,
                run,
                report.id,
                retry_failed=retry,
            )
        except DomainError as error:
            # 密钥/配置失效、结果未知：不自动重试，保留进度并告警。
            return failure(state, "cleaning_review", error)
        # 延迟补跑：15 秒、45 秒各一次，只补没有拿到判断的词根。
        for index, delay in enumerate((15.0, 45.0)):
            progress = deps.cleaning.classification_progress(project, run, report.id)
            if progress.get("status") != "failed":
                break
            stored = progress.get("next_retry_at")
            wait = delay
            if isinstance(stored, str):
                try:
                    target = datetime.fromisoformat(stored)
                    wait = max(1.0, min(60.0, (target - datetime.now(UTC)).total_seconds()))
                except ValueError:
                    wait = delay
            time.sleep(wait)
            try:
                updated = deps.cleaning.classify(project, run, report.id, retry_failed=True)
            except DomainError as error:
                if error.code in ("MODEL_NOT_CONFIGURED", "MODEL_CONFIG_INVALID", "MODEL_OUTCOME_UNKNOWN") or "鉴权" in error.message:
                    return failure(state, "cleaning_review", error)
                continue
        progress = deps.cleaning.classification_progress(project, run, report.id)
        if progress.get("status") == "failed":
            # 达到补跑上限仍未完成：保留进度并告警，不判完成，也不无限调用。
            return failure(
                state,
                "cleaning_review",
                DomainError(
                    409,
                    "CLEANING_CLASSIFICATION_INCOMPLETE",
                    "部分词根分类失败，已完成结果与待补词均已保留；稍后可从未完成处继续",
                ),
            )
        return record(
            state,
            "cleaning_review",
            "done",
            f"自动分类版本 {updated.version}",
            cleaning_version=updated.version,
        )

    def create_traffic(state: WorkflowState) -> dict[str, object]:
        project, run = ids(state)
        cleaning = gates.cleaning_any(deps, project, run, state["cleaning_id"])
        if cleaning is None or cleaning.review is None:
            return failure(
                state,
                "create_traffic",
                DomainError(409, "WORKFLOW_CLEANING_MISSING", "清洗审核尚未完成"),
            )
        progress = deps.cleaning.classification_progress(project, run, cleaning.id)
        if progress.get("status") in ("running", "failed", "outcome_pending", "not_started"):
            return failure(
                state,
                "create_traffic",
                DomainError(409, "WORKFLOW_CLEANING_MISSING", "清洗分类尚未全部完成"),
            )
        existing = gates.traffic(deps, project, run, state["traffic_id"], state["traffic_version"])
        if (
            existing is not None
            and not existing.stale
            and existing.request.cleaning_version == cleaning.version
            and existing.rules == RuleSet()
        ):
            return record(
                state,
                "create_traffic",
                "skipped",
                "已有流量筛选快照",
                traffic_id=str(existing.id),
                traffic_version=existing.version,
            )
        try:
            report = deps.traffic.create(
                project,
                run,
                TrafficRequest(cleaning_id=cleaning.id, cleaning_version=cleaning.version),
            )
        except DomainError as error:
            return failure(state, "create_traffic", error)
        return record(
            state,
            "create_traffic",
            "done",
            f"{report.total} 词待筛选",
            traffic_id=str(report.id),
            traffic_version=report.version,
        )

    def reviewed_traffic(state: WorkflowState) -> TrafficReport | None:
        project, run = ids(state)
        report = gates.traffic_any(deps, project, run, state["traffic_id"])
        if report is None or report.stale or not report.complete:
            return None
        return report

    def traffic_review(state: WorkflowState) -> dict[str, object]:
        current = reviewed_traffic(state)
        if current is not None:
            return record(
                state,
                "traffic_review",
                "skipped",
                "已完成流量审核",
                traffic_version=current.version,
            )
        if auto_decide(state):
            project, run = ids(state)
            report = gates.traffic_any(deps, project, run, state["traffic_id"])
            if report is None:
                return failure(
                    state,
                    "traffic_review",
                    DomainError(409, "WORKFLOW_TRAFFIC_MISSING", "尚无流量筛选快照"),
                )
            if report.stale:
                return failure(
                    state,
                    "traffic_review",
                    DomainError(409, "TRAFFIC_INPUT_STALE", "流量筛选快照已失效，请重新生成"),
                )
            try:
                version, reviewed_count = review_traffic_report(deps, project, run, report)
            except DomainError as error:
                latest = deps.traffic.get(project, run, report.id)
                saved = max(0, latest.reviewed - report.reviewed)
                remaining = max(0, latest.required - latest.reviewed)
                progress_error = DomainError(
                    error.status_code,
                    error.code,
                    f"{error.message}；本轮已保存 {saved} 词，剩余 {remaining} 词",
                )
                return failure(state, "traffic_review", progress_error)
            return record(
                state,
                "traffic_review",
                "done",
                f"模型审核 {reviewed_count} 词，版本 {version}",
                traffic_version=version,
            )
        payload = human_payload(
            "traffic_review",
            "审核流量入口",
            "在流量面板逐词或批量核对相关性、决定保留或排除，并填写优先级与证据。",
            refs={"traffic": state["traffic_id"] or ""},
        )
        report = await_fact(lambda: reviewed_traffic(state), payload)
        return record(
            state,
            "traffic_review",
            "done",
            f"审核版本 {report.version}",
            traffic_version=report.version,
        )

    def create_library(state: WorkflowState) -> dict[str, object]:
        project, run = ids(state)
        traffic = gates.traffic_any(deps, project, run, state["traffic_id"])
        if traffic is None or not traffic.complete:
            return failure(
                state,
                "create_library",
                DomainError(409, "WORKFLOW_TRAFFIC_MISSING", "流量审核尚未完成"),
            )
        existing = gates.library(deps, project, run, state["library_id"])
        if (
            existing is not None
            and not existing.stale
            and existing.request.traffic_id == traffic.id
            and existing.request.traffic_version == traffic.version
        ):
            if auto_decide(state) and deps.library_relevance is not None:
                deps.library_relevance.ensure(project, run, existing.id)
            return record(
                state,
                "create_library",
                "skipped",
                "已有词库草稿",
                library_id=str(existing.id),
            )
        try:
            report = deps.library.create(
                project,
                run,
                LibraryRequest(traffic_id=traffic.id, traffic_version=traffic.version),
            )
        except DomainError as error:
            return failure(state, "create_library", error)
        if auto_decide(state) and deps.library_relevance is not None:
            deps.library_relevance.ensure(project, run, report.id)
        return record(
            state,
            "create_library",
            "done",
            f"{len(report.entries)} 个词入库",
            library_id=str(report.id),
        )

    def published(state: WorkflowState) -> LibraryReport | None:
        project, run = ids(state)
        report = gates.library(deps, project, run, state["library_id"])
        if report is None or report.publication_version is None:
            return None
        return report

    def library_publish(state: WorkflowState) -> dict[str, object]:
        current = published(state)
        if current is not None:
            return record(
                state,
                "library_publish",
                "skipped",
                f"已发布 v{current.publication_version}",
                publication_version=current.publication_version,
            )
        payload = human_payload(
            "library_publish",
            "编辑并发布关键词库",
            "在词库面板检查层级、编辑优先级与辅助 ASIN 后发布。",
            refs={"library": state["library_id"] or ""},
        )
        report = await_fact(lambda: published(state), payload)
        return record(
            state,
            "library_publish",
            "done",
            f"已发布 v{report.publication_version}",
            publication_version=report.publication_version,
        )

    return {
        "create_cleaning": create_cleaning,
        "cleaning_review": cleaning_review,
        "create_traffic": create_traffic,
        "traffic_review": traffic_review,
        "create_library": create_library,
        "library_publish": library_publish,
    }
