"""工作流执行日志的纯投影函数；只保存安全、短小的业务摘要。"""

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Literal, cast

from app.domain.workflow import WorkflowEventStatus, WorkflowEventView
from app.workflows.state import NODE_LABELS

MAX_EVENTS = 200
MAX_SUMMARY_FIELDS = 20
MAX_SUMMARY_VALUE = 500
SENSITIVE_PARTS = (
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
    "prompt",
    "page_source",
    "raw_html",
)


def _now(value: datetime | None) -> datetime:
    return value or datetime.now(UTC)


def _sensitive(key: object) -> bool:
    normalized = str(key).lower().replace("-", "_")
    return any(part in normalized for part in SENSITIVE_PARTS)


def _redact(value: object) -> object:
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        return {
            str(key): "[已过滤]" if _sensitive(key) else _redact(item)
            for key, item in mapping.items()
        }
    if isinstance(value, (list, tuple, set)):
        items = cast(list[object] | tuple[object, ...] | set[object], value)
        return [_redact(item) for item in items]
    return value


def _display(value: object) -> str:
    redacted = _redact(value)
    if isinstance(redacted, str):
        rendered = redacted
    elif redacted is None:
        rendered = ""
    elif isinstance(redacted, (int, float, bool)):
        rendered = str(redacted)
    else:
        rendered = json.dumps(redacted, ensure_ascii=False, default=str)
    if len(rendered) > MAX_SUMMARY_VALUE:
        return rendered[:MAX_SUMMARY_VALUE] + "..."
    return rendered


def summarize(value: Mapping[str, object] | None) -> dict[str, str]:
    """过滤敏感键并把任意 JSON 友好值压缩为前端可直接展示的文本。"""
    if not value:
        return {}
    result: dict[str, str] = {}
    for index, (key, item) in enumerate(value.items()):
        if index >= MAX_SUMMARY_FIELDS:
            result["更多字段"] = f"已省略 {len(value) - MAX_SUMMARY_FIELDS} 项"
            break
        result[str(key)] = "[已过滤]" if _sensitive(key) else _display(item)
    return result


def _trim(events: list[WorkflowEventView]) -> list[WorkflowEventView]:
    return events[-MAX_EVENTS:]


def start_node_event(
    events: list[WorkflowEventView],
    node_key: str,
    *,
    input_data: Mapping[str, object] | None = None,
    message: str = "开始执行",
    now: datetime | None = None,
) -> list[WorkflowEventView]:
    """追加一个节点尝试；同一节点仍在等待中时沿用原事件，不重复计尝试。

    等待类节点（wait_sources / third_party）由后台轮询反复恢复执行，
    每次恢复都新起事件会让日志刷屏并把「等待」显示成「第 N 次尝试」。
    这里把「最近一条仍是等待中」的恢复视为继续等待：复用原事件、
    刷新计时起点，尝试次数保持不变。
    """
    stamp = _now(now)
    for index in range(len(events) - 1, -1, -1):
        event = events[index]
        if event.node_key != node_key:
            continue
        if event.status not in ("waiting_human", "waiting_external"):
            break
        updated = list(events)
        updated[index] = event.model_copy(
            update={
                "status": "running",
                "message": message or event.message,
                "input_summary": summarize(input_data) or event.input_summary,
                "started_at": stamp,
                "finished_at": None,
                "duration_ms": None,
            }
        )
        return updated
    sequence = max((event.sequence for event in events), default=0) + 1
    attempt = max((event.attempt for event in events if event.node_key == node_key), default=0) + 1
    event = WorkflowEventView(
        sequence=sequence,
        node_key=node_key,
        node_label=NODE_LABELS.get(node_key, node_key),
        status="running",
        attempt=attempt,
        message=message,
        input_summary=summarize(input_data),
        started_at=stamp,
    )
    return _trim([*events, event])


def _close_event(
    events: list[WorkflowEventView],
    node_key: str,
    *,
    status: WorkflowEventStatus,
    output_data: Mapping[str, object] | None,
    message: str,
    error: str,
    now: datetime | None,
) -> list[WorkflowEventView]:
    stamp = _now(now)
    updated = list(events)
    for index in range(len(updated) - 1, -1, -1):
        current = updated[index]
        if current.node_key != node_key or current.status != "running":
            continue
        duration = max(0, int((stamp - current.started_at).total_seconds() * 1000))
        updated[index] = current.model_copy(
            update={
                "status": status,
                "message": message or current.message,
                "output_summary": summarize(output_data),
                "error": error,
                "finished_at": stamp,
                "duration_ms": duration,
            }
        )
        break
    return _trim(updated)


def finish_node_event(
    events: list[WorkflowEventView],
    node_key: str,
    *,
    status: Literal["done", "failed", "skipped"] = "done",
    output_data: Mapping[str, object] | None = None,
    message: str = "执行完成",
    error: str = "",
    now: datetime | None = None,
) -> list[WorkflowEventView]:
    return _close_event(
        events,
        node_key,
        status=status,
        output_data=output_data,
        message=message,
        error=error,
        now=now,
    )


def wait_node_event(
    events: list[WorkflowEventView],
    node_key: str,
    *,
    kind: Literal["human", "external"],
    output_data: Mapping[str, object] | None = None,
    message: str = "等待继续",
    now: datetime | None = None,
) -> list[WorkflowEventView]:
    return _close_event(
        events,
        node_key,
        status="waiting_external" if kind == "external" else "waiting_human",
        output_data=output_data,
        message=message,
        error="",
        now=now,
    )


def interrupt_active_event(
    events: list[WorkflowEventView],
    reason: str,
    *,
    now: datetime | None = None,
) -> list[WorkflowEventView]:
    """关闭最后一个活动尝试，供异常、终止、取代及进程恢复路径复用。"""
    for event in reversed(events):
        if event.status == "running":
            return _close_event(
                events,
                event.node_key,
                status="interrupted",
                output_data=None,
                message="执行中断",
                error=reason,
                now=now,
            )
    return list(events)
