"""编排状态契约：只含 JSON 原语、节点清单完整、更新可合并。"""

import json
from datetime import UTC, datetime

from app.domain.workflow import WorkflowEventView
from app.workflows.state import (
    GRAPH_VERSION,
    NODE_KEYS,
    NODE_LABELS,
    initial_state,
    record,
)


def test_initial_state_is_json_serializable_and_complete() -> None:
    state = initial_state(
        workflow_id="w1", run_id="r1", project_id="p1", options={"verify_roots": False}
    )
    assert json.loads(json.dumps(state, ensure_ascii=False)) == state
    assert state["graph_version"] == GRAPH_VERSION
    assert state["revision"] == 0
    assert set(state["node_status"]) == set(NODE_KEYS)
    assert all(item["status"] == "pending" for item in state["node_status"].values())
    assert NODE_LABELS["library_publish"] == "发布"


def test_record_advances_revision_and_merges_node_status() -> None:
    state = initial_state(workflow_id="w1", run_id="r1", project_id="p1", options={})
    update = record(state, "ensure_input", "done", "输入正常", root_selection_id=None)
    assert update["revision"] == 1
    assert isinstance(update["node_status"], dict)
    json.dumps(update, ensure_ascii=False)


def test_workflow_event_contract_is_json_serializable() -> None:
    """抓住事件字段使用不可持久化类型或缺少默认摘要的回归。"""
    event = WorkflowEventView(
        sequence=1,
        node_key="ensure_input",
        node_label="校验输入",
        status="running",
        attempt=1,
        started_at=datetime(2026, 11, 1, 12, 0, tzinfo=UTC),
    )
    payload = event.model_dump(mode="json")
    assert payload["input_summary"] == {}
    assert payload["output_summary"] == {}
    json.dumps(payload, ensure_ascii=False)
