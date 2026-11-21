"""编排恢复与取代：重启后从原门继续，新实例取代旧实例。"""

from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import START, StateGraph  # pyright: ignore[reportMissingTypeStubs]
from langgraph.types import StateSnapshot
from sqlalchemy import Engine
from test_library import ready_library

from app.domain.errors import DomainError
from app.domain.workflow import WorkflowView
from app.main import create_app
from app.workflows.errors import failure
from app.workflows.state import WorkflowState, initial_state
from app.workflows.store import WorkflowStore


def _failure_snapshot(code: str) -> StateSnapshot:
    state = initial_state(workflow_id="w-error", run_id="r1", project_id="p1", options={})
    builder = StateGraph(WorkflowState)

    def fail_node(current: WorkflowState) -> dict[str, object]:
        return failure(current, "collect_core_report", DomainError(503, code, "safe hint"))

    builder.add_node("collect_core_report", fail_node)  # pyright: ignore[reportUnknownMemberType, reportArgumentType]
    builder.add_edge(START, "collect_core_report")
    graph = builder.compile(checkpointer=InMemorySaver())  # pyright: ignore[reportUnknownMemberType]
    config = {"configurable": {"thread_id": f"thread-{code}"}}
    graph.invoke(state, config)  # pyright: ignore[reportUnknownMemberType]
    return graph.get_state(config)  # pyright: ignore[reportUnknownMemberType]


@pytest.mark.parametrize(
    "code",
    [
        "AGENT_UNAVAILABLE",
        "AGENT_INVALID_RESPONSE",
        "AGENT_HTTP_ERROR",
        "AGENT_BROWSER_ERROR",
        "AGENT_FILE_MISSING",
        "AGENT_SESSION_NOT_FOUND",
    ],
)
def test_agent_network_error_pauses_for_external_recovery(code: str) -> None:
    snapshot = _failure_snapshot(code)

    assert snapshot.next == ("collect_core_report",)
    assert snapshot.values["failed_node"] is None
    payload = cast(dict[str, object], snapshot.tasks[0].interrupts[0].value)
    assert payload["kind"] == "external"
    assert cast(dict[str, str], payload["refs"])["error_code"] == code
    assert cast(dict[str, str], payload["refs"])["auto_resume"] == "1"


@pytest.mark.parametrize(
    "code",
    ["SEARCH_ACCESS_CHECK", "SELLERSPRITE_LOGIN_REQUIRED", "ABA_AUTH_REQUIRED", "ZINIAO_STORE_REQUIRED"],
)
def test_agent_attention_error_requires_human_and_disables_auto_resume(code: str) -> None:
    snapshot = _failure_snapshot(code)

    payload = cast(dict[str, object], snapshot.tasks[0].interrupts[0].value)
    assert payload["kind"] == "external"
    assert cast(dict[str, str], payload["refs"])["attention_required"] == "1"
    assert cast(dict[str, str], payload["refs"])["auto_resume"] == "0"


def test_unknown_business_error_remains_terminal_failure() -> None:
    snapshot = _failure_snapshot("UNRELATED_BUSINESS_ERROR")

    assert snapshot.next == ()
    assert snapshot.values["failed_node"] == "collect_core_report"


def test_supersede_and_recovery(db: str, engine: Engine, tmp_path: Path) -> None:
    with ready_library(db, engine, tmp_path) as (_, base, database_url, _info):
        pass

    with TestClient(create_app(database_url, storage_root=tmp_path, workflow_inline=True)) as api:
        first = WorkflowView.model_validate(
            api.post(base + "/workflow", json={"options": {"auto_decide": False}}).json()
        )
        second = WorkflowView.model_validate(
            api.post(base + "/workflow", json={"options": {"auto_decide": False}}).json()
        )
        superseded = WorkflowView.model_validate(
            api.get(base + f"/workflow/{first.workflow_id}").json()
        )
        assert superseded.status == "superseded"
        blocked = api.post(
            base + f"/workflow/{first.workflow_id}/resume",
            json={
                "gate": "week",
                "action": "continue",
                "expected_revision": first.revision,
                "payload": {"aba_week_end": "2026-10-17"},
            },
        )
        assert blocked.status_code == 409

        store = WorkflowStore(engine)
        store.initialize()
        assert store.claim(second.workflow_id, second.revision) == "ok"

    with TestClient(create_app(database_url, storage_root=tmp_path, workflow_inline=True)) as api:
        interrupted = WorkflowView.model_validate(
            api.get(base + f"/workflow/{second.workflow_id}").json()
        )
        assert interrupted.status == "interrupted"
        assert interrupted.waiting_for is not None
        assert interrupted.waiting_for.gate == "week"

        resumed = api.post(
            base + f"/workflow/{second.workflow_id}/resume",
            json={
                "gate": "week",
                "action": "continue",
                "expected_revision": interrupted.revision,
                "payload": {"aba_week_end": "2026-10-17"},
            },
        )
        assert resumed.status_code == 202, resumed.text
        view = WorkflowView.model_validate(resumed.json())
        assert view.waiting_for is not None
        assert view.waiting_for.gate == "library_publish"
