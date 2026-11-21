"""人工门与自动节点共用的事实检查；全部只读，不写业务表。"""

from collections.abc import Callable
from datetime import date
from uuid import UUID

from app.domain.aba import AbaReport
from app.domain.aba_expansion import ExpansionReport
from app.domain.cleaning import CleaningReport, CleaningRequest, Source
from app.domain.competitors import Competitors
from app.domain.core_terms import CoreSelection
from app.domain.errors import DomainError
from app.domain.library import LibraryReport
from app.domain.product_terms import ProductProfileRecord
from app.domain.product_understanding import ProductConfirmation, UnderstandingResult
from app.domain.roots import RootSelection
from app.domain.traffic import TrafficReport
from app.workflows.deps import WorkflowDeps
from app.workflows.state import WorkflowState


def optional[Value](call: Callable[[], Value]) -> Value | None:
    """把 404 视为"还没做"，其余业务错误继续上抛。"""
    try:
        return call()
    except DomainError as error:
        if error.status_code == 404:
            return None
        raise


def understanding(deps: WorkflowDeps, project: UUID, run: UUID) -> UnderstandingResult | None:
    return optional(lambda: deps.understanding.get(project, run))


def confirmation(deps: WorkflowDeps, project: UUID, run: UUID) -> ProductConfirmation | None:
    return optional(lambda: deps.confirmation.get(project, run))


def product_profile(deps: WorkflowDeps, project: UUID, run: UUID) -> ProductProfileRecord | None:
    result = optional(lambda: deps.profiles.get(project, run))
    return result if result is not None and result.status == "confirmed" else None


def roots_generated(deps: WorkflowDeps, project: UUID, run: UUID) -> bool:
    return optional(lambda: deps.roots.get(project, run)) is not None


def root_selection(deps: WorkflowDeps, project: UUID, run: UUID) -> RootSelection | None:
    return optional(lambda: deps.roots.get_selection(project, run))


def product_term_rules(deps: WorkflowDeps, project: UUID, run: UUID) -> bool:
    """候选的规则版本是分支依据；历史选择自身不携带规则版本。"""
    candidates = optional(lambda: deps.roots.get(project, run))
    return candidates is not None and candidates.rule_version == "product-term-v1"


def hot_core_rules(deps: WorkflowDeps, project: UUID, run: UUID, state: WorkflowState) -> bool:
    """第二步优先恢复自身产物；第一步商品词版本不能迁移旧核心词证据。"""
    explicit = state.get("core_rule_version")
    if explicit:
        return explicit in ("hot-search-core-v1", "hot-search-core-v2")
    selected = core_selection(deps, project, run)
    if selected is not None:
        return selected.rule_version in ("hot-search-core-v1", "hot-search-core-v2")
    report = aba_report(deps, project, run, state.get("aba_report_id"))
    if report is not None:
        return report.query_mode == "product_term"
    if state.get("hot_search_core_analysis_id"):
        return True
    analysis_id = state.get("core_analysis_id")
    if analysis_id:
        legacy = optional(lambda: deps.core.get(project, run, UUID(analysis_id)))
        if legacy is not None:
            return False
        hot = optional(lambda: deps.hot_search_core_analysis.get(project, run, UUID(analysis_id)))
        if hot is not None:
            return True
    return product_term_rules(deps, project, run)


def aba_report(deps: WorkflowDeps, project: UUID, run: UUID, key: str | None) -> AbaReport | None:
    if not key:
        return None
    return optional(lambda: deps.aba.get(project, run, UUID(key)))


def core_selection(deps: WorkflowDeps, project: UUID, run: UUID) -> CoreSelection | None:
    return optional(lambda: deps.core.selection(project, run))


def competitors(deps: WorkflowDeps, project: UUID, run: UUID) -> Competitors | None:
    return optional(lambda: deps.competitors.get(project, run))


def expansion(
    deps: WorkflowDeps, project: UUID, run: UUID, key: str | None
) -> ExpansionReport | None:
    if not key:
        return None
    return optional(lambda: deps.expansion.get(project, run, UUID(key)))


def latest_expansion(
    deps: WorkflowDeps, project: UUID, run: UUID, direction: str, week: date
) -> ExpansionReport | None:
    """同周最新一份未过期拓词报告；中途启动编排时直接复用。"""
    workspace = deps.expansion.workspace(project, run, week)
    return next((item for item in workspace.reports if item.direction == direction), None)


def cleaning_sources(
    deps: WorkflowDeps, project: UUID, run: UUID, request: CleaningRequest
) -> list[Source] | None:
    """四路是否齐全由清洗服务判定；409 表示还没齐，不是故障。"""
    try:
        return deps.cleaning.sources(project, run, request)
    except DomainError as error:
        if error.status_code == 409:
            return None
        raise


def cleaning_any(
    deps: WorkflowDeps, project: UUID, run: UUID, key: str | None
) -> CleaningReport | None:
    if not key:
        return None
    return optional(lambda: deps.cleaning.get(project, run, UUID(key)))


def cleaning(
    deps: WorkflowDeps, project: UUID, run: UUID, key: str | None, version: int | None
) -> CleaningReport | None:
    if not key or version is None:
        return None
    report = optional(lambda: deps.cleaning.get(project, run, UUID(key)))
    if report is None or report.version != version:
        return None
    return report


def traffic_any(
    deps: WorkflowDeps, project: UUID, run: UUID, key: str | None
) -> TrafficReport | None:
    if not key:
        return None
    return optional(lambda: deps.traffic.get(project, run, UUID(key)))


def traffic(
    deps: WorkflowDeps, project: UUID, run: UUID, key: str | None, version: int | None
) -> TrafficReport | None:
    if not key or version is None:
        return None
    report = optional(lambda: deps.traffic.get(project, run, UUID(key)))
    if report is None or report.version != version:
        return None
    return report


def library(deps: WorkflowDeps, project: UUID, run: UUID, key: str | None) -> LibraryReport | None:
    if not key:
        return None
    return optional(lambda: deps.library.get(project, run, UUID(key)))
