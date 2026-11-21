"""执行日志契约：生命周期、重试、容量和安全摘要。"""

from datetime import UTC, datetime, timedelta

from app.domain.workflow import WorkflowView
from app.workflows.events import (
    finish_node_event,
    interrupt_active_event,
    start_node_event,
    summarize,
    wait_node_event,
)

BASE = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def test_old_workflow_view_defaults_events_to_empty() -> None:
    """抓住旧 JSON 因缺少 events 而无法读取的回归。"""
    view = WorkflowView.model_validate(
        {
            "workflow_id": "w1",
            "run_id": "r1",
            "project_id": "p1",
            "status": "running",
            "revision": 0,
            "created_at": BASE.isoformat(),
            "updated_at": BASE.isoformat(),
        }
    )
    assert view.events == []


def test_event_lifecycle_records_output_duration_wait_and_failure() -> None:
    """抓住节点完成后仍显示 running、缺少输出或耗时的回归。"""
    events = start_node_event([], "ensure_input", input_data={"run_id": "r1"}, now=BASE)
    assert events[0].model_dump() | {} == {
        "sequence": 1,
        "node_key": "ensure_input",
        "node_label": "校验输入",
        "status": "running",
        "attempt": 1,
        "message": "开始执行",
        "input_summary": {"run_id": "r1"},
        "output_summary": {},
        "error": "",
        "started_at": BASE,
        "finished_at": None,
        "duration_ms": None,
    }
    events = finish_node_event(
        events,
        "ensure_input",
        output_data={"result": "输入正常", "rows": 12},
        message="输入正常",
        now=BASE + timedelta(milliseconds=1250),
    )
    assert events[0].status == "done"
    assert events[0].output_summary == {"result": "输入正常", "rows": "12"}
    assert events[0].duration_ms == 1250

    events = start_node_event(events, "root_selection", now=BASE + timedelta(seconds=2))
    events = wait_node_event(
        events,
        "root_selection",
        kind="human",
        message="等待确认词根",
        output_data={"candidates": 8},
        now=BASE + timedelta(seconds=3),
    )
    assert events[-1].status == "waiting_human"
    assert events[-1].output_summary == {"candidates": "8"}

    events = start_node_event(events, "create_library", now=BASE + timedelta(seconds=4))
    events = finish_node_event(
        events,
        "create_library",
        status="failed",
        error="生成失败",
        now=BASE + timedelta(seconds=5),
    )
    assert events[-1].status == "failed"
    assert events[-1].error == "生成失败"


def test_retry_increments_attempt_and_preserves_previous_attempt() -> None:
    """抓住重试覆盖旧尝试、或 attempt 始终为 1 的回归。"""
    events = start_node_event([], "core_terms", now=BASE)
    events = finish_node_event(events, "core_terms", status="failed", error="模型超时", now=BASE)
    events = start_node_event(events, "core_terms", now=BASE + timedelta(seconds=1))
    assert [(item.attempt, item.status) for item in events] == [
        (1, "failed"),
        (2, "running"),
    ]
    assert [item.sequence for item in events] == [1, 2]


def test_events_keep_only_latest_200_and_interrupt_active_attempt() -> None:
    """抓住日志无限增长及服务重启后永久显示执行中的回归。"""
    events = []
    for _index in range(205):
        events = start_node_event(events, "ensure_input", now=BASE)
        events = finish_node_event(events, "ensure_input", now=BASE)
    assert len(events) == 200
    assert events[0].sequence == 6
    assert events[-1].sequence == 205

    events = start_node_event(events, "create_library", now=BASE)
    interrupted = interrupt_active_event(events, "服务重启", now=BASE)
    assert interrupted[-1].status == "interrupted"
    assert interrupted[-1].error == "服务重启"


def test_summary_redacts_sensitive_keys_and_truncates_large_values() -> None:
    """抓住日志泄露认证信息、模型提示词或大块网页的回归。"""
    summary = summarize(
        {
            "token": "secret-token",
            "Cookie": "session=private",
            "prompt": "完整模型提示词",
            "nested": {"authorization": "Bearer private", "ok": "visible"},
            "description": "x" * 900,
        }
    )
    rendered = str(summary)
    assert "secret-token" not in rendered
    assert "session=private" not in rendered
    assert "完整模型提示词" not in rendered
    assert "Bearer private" not in rendered
    assert "visible" in rendered
    assert len(summary["description"]) <= 503
