from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import Engine, text

from app.domain.workflow import WaitingForView, WorkflowNodeView, WorkflowView
from app.workflows.agent_recovery import AgentRecoveryScheduler
from app.workflows.browser_operations import BrowserOperationStore
from app.workflows.state import NODE_KEYS, NODE_LABELS, initial_state
from app.workflows.store import WorkflowRecord, WorkflowStore


class RecordingRunner:
    def __init__(self) -> None:
        self.resumed: list[str] = []

    def resume(self, record: WorkflowRecord, request: object) -> None:
        self.resumed.append(record.workflow_id)


def _waiting(
    engine: Engine, *, attention: bool = False, background: bool = False
) -> tuple[WorkflowStore, BrowserOperationStore]:
    stamp = datetime.now(UTC).isoformat()
    state = initial_state(workflow_id="workflow-1", run_id="run-1", project_id="project-1", options={})
    waiting = WaitingForView(
        kind="external",
        gate="collect_core_report",
        title="等待处理",
        actions=["continue"] if attention else ["poll", "continue"],
        refs={
            "error_code": "SEARCH_ACCESS_CHECK" if attention else "AGENT_UNAVAILABLE",
            "attention_required": "1" if attention else "0",
            "auto_resume": "0" if attention else "1",
            "background_poll": "1" if background else "0",
        },
        ready_to_resume=not attention,
    )
    view = WorkflowView(
        workflow_id="workflow-1",
        run_id="run-1",
        project_id="project-1",
        status="waiting_external",
        revision=0,
        current_node="collect_core_report",
        nodes=[
            WorkflowNodeView(key=key, label=NODE_LABELS[key], status="pending")
            for key in NODE_KEYS
        ],
        waiting_for=waiting,
        created_at=datetime.fromisoformat(stamp),
        updated_at=datetime.fromisoformat(stamp),
    )
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO projects (id,document) VALUES ('project-1','{}')"))
        connection.execute(
            text(
                "INSERT INTO pipeline_runs (id,project_id,idempotency_key,input_hash,document) "
                "VALUES ('run-1','project-1','input-1','hash-1','{}')"
            )
        )
    workflows = WorkflowStore(engine)
    workflows.create(
        workflow_id="workflow-1",
        run_id="run-1",
        project_id="project-1",
        state=json.dumps(state),
        view=view.model_dump_json(),
        created_at=stamp,
    )
    workflows.save(
        workflow_id="workflow-1",
        status="waiting_external",
        revision=0,
        state=json.dumps(state),
        view=view.model_dump_json(),
        now=stamp,
    )
    operations = BrowserOperationStore(engine)
    operation = operations.begin(
        workflow_id="workflow-1",
        run_id="run-1",
        node_key="collect_core_report",
        source="aba",
        idempotency_key="week-2026-10-24",
        request_summary={"week": "2026-10-24"},
    )
    operations.wait(
        operation.operation_id,
        error_code=waiting.refs["error_code"],
        allow_auto_resume=not attention and not background,
    )
    return workflows, operations


def test_offline_probe_does_not_consume_resume_budget(engine: Engine) -> None:
    workflows, operations = _waiting(engine)
    runner = RecordingRunner()
    scheduler = AgentRecoveryScheduler(workflows, operations, cast(object, runner), lambda: "offline")

    assert scheduler.tick() == 0
    saved = operations.waiting()[0]
    assert saved.auto_resume_remaining == 1
    assert runner.resumed == []


def test_online_probe_resumes_once_and_survives_new_store(engine: Engine) -> None:
    workflows, operations = _waiting(engine)
    runner = RecordingRunner()
    scheduler = AgentRecoveryScheduler(
        WorkflowStore(engine), BrowserOperationStore(engine), cast(object, runner), lambda: "online"
    )

    assert scheduler.tick() == 1
    assert scheduler.tick() == 0
    assert runner.resumed == ["workflow-1"]
    saved = operations.get_by_key(
        run_id="run-1",
        node_key="collect_core_report",
        source="aba",
        idempotency_key="week-2026-10-24",
    )
    assert saved is not None
    assert saved.auto_resume_remaining == 0


def test_concurrent_schedulers_claim_only_once(engine: Engine) -> None:
    workflows, operations = _waiting(engine)
    runner = RecordingRunner()
    first = AgentRecoveryScheduler(workflows, operations, cast(object, runner), lambda: "online")
    second = AgentRecoveryScheduler(
        WorkflowStore(engine), BrowserOperationStore(engine), cast(object, runner), lambda: "online"
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda scheduler: scheduler.tick(), (first, second)))

    assert sorted(results) == [0, 1]
    assert runner.resumed == ["workflow-1"]


def test_attention_required_never_auto_resumes(engine: Engine) -> None:
    workflows, operations = _waiting(engine, attention=True)
    runner = RecordingRunner()
    scheduler = AgentRecoveryScheduler(workflows, operations, cast(object, runner), lambda: "online")

    assert scheduler.tick() == 0
    assert runner.resumed == []


def test_background_completion_polling_does_not_use_network_resume_budget(
    engine: Engine,
) -> None:
    workflows, operations = _waiting(engine, background=True)
    runner = RecordingRunner()
    scheduler = AgentRecoveryScheduler(
        workflows, operations, cast(object, runner), lambda: "offline"
    )

    assert scheduler.tick() == 1
    assert runner.resumed == ["workflow-1"]
    operation = operations.get_by_key(
        run_id="run-1",
        node_key="collect_core_report",
        source="aba",
        idempotency_key="week-2026-10-24",
    )
    assert operation is not None
    assert operation.auto_resume_remaining == 0
