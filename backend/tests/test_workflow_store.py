"""编排注册表：创建、抢占、历史、取代与重启恢复。"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine

from app.domain.errors import DomainError
from app.domain.workflow import WorkflowEventView
from app.workflows.store import WorkflowStore

VIEW = '{"status": "waiting_human", "waiting_for": {"gate": "g"}}'


def running_view() -> str:
    event = WorkflowEventView(
        sequence=1,
        node_key="core_terms",
        node_label="核心大词",
        status="running",
        attempt=1,
        started_at=datetime(2026, 10, 26, 4, 59, tzinfo=UTC),
    )
    return '{"status":"running","events":[' + event.model_dump_json() + "]}"


def build(engine: Engine) -> WorkflowStore:
    item = WorkflowStore(engine)
    item.initialize()
    return item


def create(items: WorkflowStore, key: str, stamp: str) -> None:
    items.create(
        workflow_id=key,
        run_id="r1",
        project_id="p1",
        state="{}",
        view=VIEW,
        created_at=f"2026-10-26T{stamp}:00:00Z",
    )


def test_create_get_latest_and_history(engine: Engine) -> None:
    items = build(engine)
    create(items, "w1", "00")
    create(items, "w2", "01")
    assert items.get("w1") is not None
    latest = items.latest("p1", "r1")
    assert latest is not None and latest.workflow_id == "w2"
    assert [item.workflow_id for item in items.history("p1", "r1")] == ["w2", "w1"]
    assert items.latest("other", "r1") is None


def test_claim_checks_status_and_revision(engine: Engine) -> None:
    items = build(engine)
    create(items, "w1", "00")
    assert items.claim("missing", 0) == "missing"
    assert items.claim("w1", 1) == "revision"
    assert items.claim("w1", 0) == "ok"
    assert items.claim("w1", 0) == "state"


def test_save_and_supersede(engine: Engine) -> None:
    items = build(engine)
    create(items, "w1", "01")
    create(items, "w2", "02")
    items.save(
        workflow_id="w1",
        status="waiting_human",
        revision=3,
        state='{"revision":3}',
        view=VIEW,
        now="2026-10-26T03:00:00Z",
    )
    saved = items.get("w1")
    assert saved is not None and saved.revision == 3
    items.mark_superseded("p1", "r1", "w2", "2026-10-26T04:00:00Z")
    superseded = items.get("w1")
    assert superseded is not None
    assert superseded.status == "superseded" and superseded.superseded_by == "w2"
    active = items.get("w2")
    assert active is not None and active.status == "queued"


def test_recover_running_marks_interrupted(engine: Engine) -> None:
    items = build(engine)
    create(items, "w1", "00")
    assert items.claim("w1", 0) == "ok"
    current = items.get("w1")
    assert current is not None
    items.save(
        workflow_id="w1",
        status="running",
        revision=0,
        state=current.state,
        view=running_view(),
        now="2026-10-26T04:59:00Z",
    )
    assert items.recover_running("2026-10-26T05:00:00Z") == ["w1"]
    recovered = items.get("w1")
    assert recovered is not None
    assert recovered.status == "interrupted"
    assert '"interrupted"' in recovered.view
    payload = __import__("json").loads(recovered.view)
    assert payload["events"][0]["status"] == "interrupted"
    assert payload["events"][0]["finished_at"] is not None


def test_exclusive_start_blocks_active_execution(engine: Engine) -> None:
    items = build(engine)
    create(items, "w1", "00")
    with pytest.raises(DomainError) as error:
        items.create(
            workflow_id="w2",
            run_id="r1",
            project_id="p1",
            state="{}",
            view=VIEW,
            created_at="2026-10-26T01:00:00Z",
            exclusive=True,
        )
    assert error.value.code == "WORKFLOW_ALREADY_RUNNING"
    assert items.get("w2") is None
