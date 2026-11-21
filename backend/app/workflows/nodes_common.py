"""节点公用：门载荷、等待与节点类型。"""

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import cast
from uuid import UUID

from langgraph.types import interrupt

from app.domain.cleaning import CleaningRequest
from app.domain.errors import DomainError
from app.domain.product_understanding import ProductUnderstanding
from app.services.qwen import QwenClient, QwenConfig
from app.workflows import gates
from app.workflows.deps import WorkflowDeps
from app.workflows.state import WorkflowState

Payload = dict[str, object]
Node = Callable[[WorkflowState], dict[str, object]]


def ids(state: WorkflowState) -> tuple[UUID, UUID]:
    return UUID(state["project_id"]), UUID(state["run_id"])


def option_bool(state: WorkflowState, name: str, default: bool) -> bool:
    value = state["options"].get(name)
    return value if isinstance(value, bool) else default


def option_int(state: WorkflowState, name: str, default: int) -> int:
    value = state["options"].get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def auto_decide(state: WorkflowState) -> bool:
    """自动判断模式；关闭后退回逐步人工确认。"""
    return option_bool(state, "auto_decide", True)


def own_brands(state: WorkflowState) -> list[str]:
    value = state["options"].get("own_brands")
    if not isinstance(value, list):
        return []
    return [item for item in cast(list[object], value) if isinstance(item, str)]


def model_client(deps: WorkflowDeps) -> QwenClient:
    if deps.model is not None:
        return deps.model
    client = QwenClient(
        QwenConfig.load(deps.repository.storage_root),
        diagnostic_dir=deps.repository.storage_root / "model_diagnostics",
    )
    client.config.validate()
    return client


def latest_saturday() -> date:
    """最新已结束报告周的周六，与三方最近30天窗口独立。"""
    today = datetime.now(UTC).date()
    return today - timedelta(days=(today.weekday() - 5) % 7 or 7)


def product_of(deps: WorkflowDeps, project: UUID, run: UUID) -> ProductUnderstanding:
    confirmation = gates.confirmation(deps, project, run)
    if confirmation is not None:
        return confirmation.data.product
    understanding = gates.understanding(deps, project, run)
    if understanding is None:
        raise DomainError(409, "PRODUCT_UNDERSTANDING_REQUIRED", "尚无商品识别结果")
    return understanding.product


def week_end(state: WorkflowState) -> date:
    raw = state["aba_week_end"]
    if not raw:
        raise DomainError(409, "WORKFLOW_WEEK_REQUIRED", "尚未选择 ABA 报告周")
    return date.fromisoformat(raw)


def cleaning_request(state: WorkflowState) -> CleaningRequest:
    # 两种来源只合并关键词文本：ABA 按所选周，卖家精灵保持最近 30 天口径。
    # 不为最近 30 天推算供应商未声明的起止日期，也不把第三方指标变成周报指标。
    return CleaningRequest(
        aba_week_end=week_end(state),
        seller_period="last_30_days",
        text_only_acknowledged=True,
    )


def human_payload(
    gate: str,
    title: str,
    instructions: str = "",
    refs: dict[str, str] | None = None,
    actions: list[str] | None = None,
) -> Payload:
    return {
        "kind": "human",
        "gate": gate,
        "title": title,
        "instructions": instructions,
        "actions": actions or ["continue"],
        "refs": refs or {},
        "ready_to_resume": False,
    }


def external_payload(
    gate: str,
    title: str,
    instructions: str = "",
    refs: dict[str, str] | None = None,
    ready: bool = False,
) -> Payload:
    return {
        "kind": "external",
        "gate": gate,
        "title": title,
        "instructions": instructions,
        "actions": ["poll"],
        "refs": refs or {},
        "ready_to_resume": ready,
    }


def await_fact[Value](check: Callable[[], Value | None], payload: Payload) -> Value:
    """满足即返回；否则暂停。resume 后节点从头执行，仍不满足就再次暂停。"""
    while True:
        # 先查业务库中的真实保存结果；点击“继续”的载荷本身不代表审核已经完成。
        value = check()
        if value is not None:
            return value
        interrupt(payload)
