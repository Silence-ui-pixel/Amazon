from __future__ import annotations

from io import StringIO

import httpx
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text

from app.adapters.remote_browser import AgentClient
from app.domain.browser_operations import BrowserOperation
from app.workflows.browser_operations import BrowserOperationStore, wrap_browser_operation
from app.workflows.state import initial_state


def _store(engine: Engine) -> BrowserOperationStore:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO projects (id,document) VALUES ('project-1','{}') "
                "ON CONFLICT DO NOTHING"
            )
        )
        connection.execute(
            text(
                "INSERT INTO pipeline_runs (id,project_id,idempotency_key,input_hash,document) "
                "VALUES ('run-1','project-1','input-1','hash-1','{}') ON CONFLICT DO NOTHING"
            )
        )
        connection.execute(
            text(
                "INSERT INTO workflow_runs "
                "(workflow_id,run_id,project_id,status,revision,state,view,created_at,updated_at) "
                "VALUES ('workflow-1','run-1','project-1','running',0,'{}','{}','now','now') "
                "ON CONFLICT DO NOTHING"
            )
        )
    return BrowserOperationStore(engine)


def _begin(
    store: BrowserOperationStore, *, key: str = "week-2026-10-24"
) -> BrowserOperation:
    return store.begin(
        workflow_id="workflow-1",
        run_id="run-1",
        node_key="collect_core_report",
        source="aba",
        idempotency_key=key,
        request_summary={"报告周": "2026-10-24", "token": "must-not-persist"},
    )


def test_begin_reuses_one_operation_for_same_business_key(engine: Engine) -> None:
    store = _store(engine)

    first = _begin(store)
    second = _begin(store)

    assert second.operation_id == first.operation_id
    assert second.status == "running"
    assert second.attempts == 1
    assert "must-not-persist" not in second.request_summary
    assert "[已过滤]" in second.request_summary


def test_result_is_successful_only_after_explicit_commit(engine: Engine) -> None:
    store = _store(engine)
    operation = _begin(store)
    assert operation.status == "running"

    store.succeed(operation.operation_id, result_ref="aba-report-123")
    store.succeed(operation.operation_id, result_ref="aba-report-123")

    saved = store.get(operation.operation_id)
    assert saved is not None
    assert saved.status == "succeeded"
    assert saved.result_ref == "aba-report-123"
    assert saved.attempts == 1


def test_waiting_operation_has_one_atomic_auto_resume(engine: Engine) -> None:
    store = _store(engine)
    operation = _begin(store)
    store.wait(operation.operation_id, error_code="AGENT_UNAVAILABLE")

    assert store.consume_auto_resume(operation.operation_id) is True
    assert store.consume_auto_resume(operation.operation_id) is False

    saved = store.get(operation.operation_id)
    assert saved is not None
    assert saved.status == "running"
    assert saved.auto_resume_remaining == 0
    assert saved.attempts == 2


def test_operation_survives_new_store_instance(engine: Engine) -> None:
    first_store = _store(engine)
    operation = _begin(first_store, key="persistent-key")
    first_store.wait(operation.operation_id, error_code="AGENT_BROWSER_ERROR")

    restored = BrowserOperationStore(engine).get_by_key(
        run_id="run-1",
        node_key="collect_core_report",
        source="aba",
        idempotency_key="persistent-key",
    )

    assert restored is not None
    assert restored.operation_id == operation.operation_id
    assert restored.status == "waiting"
    assert restored.error_code == "AGENT_BROWSER_ERROR"


def test_offline_migration_creates_browser_operations_once() -> None:
    """抓住初始迁移动态读取当前 metadata，导致新表在两版迁移中重复创建。"""
    output = StringIO()
    config = Config("alembic.ini", output_buffer=output)

    command.upgrade(config, "head", sql=True)

    sql = output.getvalue()
    assert sql.count("CREATE TABLE browser_operations") == 1
    assert "UNIQUE (run_id, node_key, source, idempotency_key)" in sql


def test_remote_node_wrapper_persists_id_and_sets_agent_context(engine: Engine) -> None:
    """抓住操作 ID 只写数据库、却没进入节点状态或 Agent 请求头的回归。"""
    store = _store(engine)
    received: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received.append(request.headers.get("X-Keyword-Operation-ID"))
        return httpx.Response(200, json={"result": {"ok": True}})

    client = AgentClient(
        "http://agent",
        token="agent-token-0123456789abcdef-0123456789",
        transport=httpx.MockTransport(handler),
    )
    state = initial_state(
        workflow_id="workflow-1", run_id="run-1", project_id="project-1", options={}
    )
    state["aba_week_end"] = "2026-10-24"

    def remote_node(_state: object) -> dict[str, object]:
        client.call("GET", "/v1/status")
        return {"aba_report_id": "aba-report-123"}

    wrapped = wrap_browser_operation("collect_core_report", remote_node, store)
    result = wrapped(state)
    operation_ids = result["browser_operation_ids"]
    assert isinstance(operation_ids, dict)
    operation_id = str(operation_ids["collect_core_report"])

    assert received == [operation_id]
    saved = store.get(operation_id)
    assert saved is not None
    assert saved.status == "running"


def test_remote_node_wrapper_keeps_legacy_checkpoint_without_workflow_row(
    engine: Engine,
) -> None:
    """旧检查点没有 workflow_runs 投影时，不得创建无法恢复的孤立操作。"""
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO projects (id,document) VALUES ('legacy-project','{}') "
                "ON CONFLICT DO NOTHING"
            )
        )
        connection.execute(
            text(
                "INSERT INTO pipeline_runs (id,project_id,idempotency_key,input_hash,document) "
                "VALUES ('legacy-run','legacy-project','legacy-input','legacy-hash','{}') "
                "ON CONFLICT DO NOTHING"
            )
        )
    store = BrowserOperationStore(engine)
    state = initial_state(
        workflow_id="legacy",
        run_id="legacy-run",
        project_id="legacy-project",
        options={},
    )

    wrapped = wrap_browser_operation(
        "collect_core_report",
        lambda _state: {"aba_report_id": "legacy-report"},
        store,
    )

    assert wrapped(state) == {"aba_report_id": "legacy-report"}
