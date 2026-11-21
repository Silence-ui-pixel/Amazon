"""把图状态与 pending interrupt 投影成 API 视图。"""

import json
from datetime import datetime
from typing import cast

from app.domain.workflow import (
    NodeState,
    WaitingForView,
    WorkflowEventView,
    WorkflowNodeView,
    WorkflowStatus,
    WorkflowView,
)
from app.workflows.protocols import SnapshotView
from app.workflows.state import NODE_KEYS, NODE_LABELS, WorkflowState
from app.workflows.store import WorkflowRecord

POLL_MS: dict[str, int] = {
    "running": 1500,
    "waiting_external": 5000,
    "waiting_human": 30000,
}


def pending(snapshot: SnapshotView) -> tuple[str | None, WaitingForView | None]:
    """读取当前暂停的节点与 interrupt 载荷；没有暂停时返回空。"""
    for task in snapshot.tasks:
        for item in task.interrupts:
            value = item.value
            if not isinstance(value, dict):
                continue
            try:
                return task.name, WaitingForView.model_validate(value)
            except ValueError:
                continue
    return None, None


def _status(
    state: WorkflowState,
    snapshot: SnapshotView,
    waiting: WaitingForView | None,
    override: WorkflowStatus | None,
) -> WorkflowStatus:
    if override is not None:
        return override
    if waiting is not None:
        return "waiting_external" if waiting.kind == "external" else "waiting_human"
    if state["failed_node"] and not snapshot.next:
        return "failed"
    if not snapshot.next:
        return "completed"
    return "running"


def build_view(
    record: WorkflowRecord,
    state: WorkflowState,
    snapshot: SnapshotView,
    override: WorkflowStatus | None = None,
    events: list[WorkflowEventView] | None = None,
) -> WorkflowView:
    # 展示状态由检查点、暂停载荷和节点记录合成；不能仅靠某一步有结果就宣称全图完成。
    paused, waiting = pending(snapshot)
    status = _status(state, snapshot, waiting, override)
    if events is None:
        try:
            decoded = cast(object, json.loads(record.view))
            if not isinstance(decoded, dict):
                raise ValueError("workflow view must be an object")
            stored = cast(dict[str, object], decoded)
            raw_events = stored.get("events", [])
            if not isinstance(raw_events, list):
                raise ValueError("workflow events must be a list")
            events = [
                WorkflowEventView.model_validate(item) for item in cast(list[object], raw_events)
            ]
        except (TypeError, ValueError, json.JSONDecodeError):
            events = []
    nodes: list[WorkflowNodeView] = []
    for key in NODE_KEYS:
        entry = state["node_status"].get(key, {"status": "pending", "detail": "", "updated_at": ""})
        node_status: NodeState = entry["status"]
        if key == paused and waiting is not None:
            node_status = "waiting_external" if waiting.kind == "external" else "waiting_human"
        elif status == "running" and snapshot.next and key == snapshot.next[0]:
            node_status = "running"
        nodes.append(
            WorkflowNodeView(
                key=key,
                label=NODE_LABELS[key],
                status=node_status,
                detail=entry["detail"],
            )
        )
    return WorkflowView(
        workflow_id=record.workflow_id,
        run_id=record.run_id,
        project_id=record.project_id,
        status=status,
        revision=state["revision"],
        current_node=snapshot.next[0] if snapshot.next else None,
        nodes=nodes,
        events=events,
        waiting_for=waiting,
        error=state["error"],
        next_poll_ms=POLL_MS.get(cast(str, status), 0),
        created_at=datetime.fromisoformat(record.created_at),
        updated_at=datetime.fromisoformat(record.updated_at),
    )
