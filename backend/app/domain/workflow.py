"""编排接口契约：节点视图、启动与恢复请求、等待原因。"""

from datetime import UTC, date, datetime
from typing import Literal

from pydantic import Field, field_validator

from app.domain.schemas import Contract

NodeState = Literal[
    "pending",
    "running",
    "waiting_human",
    "waiting_external",
    "done",
    "failed",
    "skipped",
]

WorkflowEventStatus = Literal[
    "running",
    "waiting_human",
    "waiting_external",
    "done",
    "failed",
    "skipped",
    "interrupted",
]

WorkflowStatus = Literal[
    "queued",
    "running",
    "waiting_human",
    "waiting_external",
    "completed",
    "failed",
    "interrupted",
    "superseded",
    "aborted",
    "incompatible",
]


class WorkflowOptions(Contract):
    """启动参数；auto_decide 关闭时退回逐步人工确认。"""

    auto_decide: bool = True
    manual_cleaning_review: bool = False
    reanalyze_core: bool = False
    report_week_end: date | None = None
    verify_roots: bool = False
    own_brands: list[str] = Field(default_factory=list[str], max_length=20)
    core_max_pages: int = Field(default=50, ge=1, le=50)
    third_party_mode: Literal["manual", "auto"] = "manual"
    deadline_hours: int = Field(default=72, ge=1, le=720)

    @field_validator("report_week_end")
    @classmethod
    def finished_week(cls, value: date | None) -> date | None:
        if value is not None and (value.weekday() != 5 or value >= datetime.now(UTC).date()):
            raise ValueError("请选择已结束报告周的周六")
        return value


class StartWorkflowRequest(Contract):
    options: WorkflowOptions = Field(default_factory=WorkflowOptions)
    # 该任务使用哪台访问者设备；留空表示沿用服务器默认 Agent（单操作员过渡）。
    browser_device_id: str | None = Field(default=None, max_length=200)


class ResumeRequest(Contract):
    """人工门继续或外部等待轮询；expected_revision 防止并发提交。"""

    gate: str = Field(min_length=1, max_length=100)
    action: Literal["continue", "poll", "abort"] = "continue"
    expected_revision: int = Field(ge=0)
    payload: dict[str, str] = Field(default_factory=dict[str, str])


class WorkflowNodeView(Contract):
    key: str
    label: str
    status: NodeState
    detail: str = ""


class WorkflowEventView(Contract):
    """单次节点尝试的持久化执行摘要。"""

    sequence: int = Field(ge=1)
    node_key: str
    node_label: str
    status: WorkflowEventStatus
    attempt: int = Field(ge=1)
    message: str = ""
    input_summary: dict[str, str] = Field(default_factory=dict[str, str])
    output_summary: dict[str, str] = Field(default_factory=dict[str, str])
    error: str = ""
    started_at: datetime
    finished_at: datetime | None = None
    duration_ms: int | None = Field(default=None, ge=0)


class WaitingForView(Contract):
    kind: Literal["human", "external", "stale"]
    gate: str
    title: str
    instructions: str = ""
    actions: list[str] = Field(default_factory=list[str])
    refs: dict[str, str] = Field(default_factory=dict[str, str])
    ready_to_resume: bool = False


class WorkflowView(Contract):
    workflow_id: str
    run_id: str
    project_id: str
    status: WorkflowStatus
    revision: int
    current_node: str | None = None
    nodes: list[WorkflowNodeView] = Field(default_factory=list[WorkflowNodeView])
    events: list[WorkflowEventView] = Field(default_factory=list[WorkflowEventView])
    waiting_for: WaitingForView | None = None
    error: str = ""
    next_poll_ms: int = 5000
    created_at: datetime
    updated_at: datetime
