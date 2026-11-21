"""付费执行必须等 queued 分析检查点写入成功；真实图和 runner 的时序契约。"""

import json
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import cast
from uuid import UUID

import httpx
import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import ChannelVersions, Checkpoint, CheckpointMetadata
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph  # pyright: ignore[reportMissingTypeStubs]
from sqlalchemy import Engine
from test_workflow_auto import hot_workflow

from app.adapters.remote_browser import AgentClient
from app.domain.browser_operations import BrowserOperation
from app.domain.workflow import WorkflowNodeView, WorkflowOptions, WorkflowView
from app.services.hot_search_core_analysis import HotSearchCoreAnalysis
from app.workflows.nodes_steps_1_2 import build_nodes_1_2
from app.workflows.protocols import CompiledWorkflow
from app.workflows.runner import WorkflowRunner
from app.workflows.service import WorkflowService
from app.workflows.state import NODE_KEYS, NODE_LABELS, WorkflowState, initial_state
from app.workflows.store import WorkflowRecord, WorkflowStore


class StreamingGraph:
    """两个快速连续节点的可控图；每次 yield 前更新 checkpoint。"""

    def __init__(self, state: WorkflowState) -> None:
        self.state = state
        self.next: tuple[str, ...] = ("ensure_input",)

    def stream(
        self, value: object, config: RunnableConfig, **kwargs: object
    ) -> Iterator[dict[str, dict[str, int]]]:
        assert kwargs == {"stream_mode": "updates", "durability": "sync"}
        self.state["revision"] = 1
        self.state["node_status"]["ensure_input"] = {
            "status": "done",
            "detail": "输入正常，共 2 张图片",
            "updated_at": "2026-11-01T12:00:01+00:00",
        }
        self.next = ("identify_product",)
        yield {"ensure_input": {"revision": 1}}
        self.state["revision"] = 2
        self.state["node_status"]["identify_product"] = {
            "status": "done",
            "detail": "识别完成，候选 3 个",
            "updated_at": "2026-11-01T12:00:02+00:00",
        }
        self.next = ()
        yield {"identify_product": {"revision": 2}}

    def get_state(self, config: RunnableConfig) -> SimpleNamespace:
        return SimpleNamespace(values=self.state, next=self.next, tasks=())


class FailingGraph(StreamingGraph):
    def stream(
        self, value: object, config: RunnableConfig, **kwargs: object
    ) -> Iterator[dict[str, dict[str, int]]]:
        if False:
            yield {}
        raise RuntimeError("controlled graph failure")


class RecordingStore:
    def __init__(self, record: WorkflowRecord) -> None:
        self.record = record
        self.views: list[WorkflowView] = []

    def get(self, workflow_id: str) -> WorkflowRecord | None:
        return self.record if workflow_id == self.record.workflow_id else None

    def save(self, **payload: object) -> None:
        self.record = WorkflowRecord(
            workflow_id=self.record.workflow_id,
            run_id=self.record.run_id,
            project_id=self.record.project_id,
            status=str(payload["status"]),
            revision=cast(int, payload["revision"]),
            state=str(payload["state"]),
            view=str(payload["view"]),
            created_at=self.record.created_at,
            updated_at=str(payload["now"]),
            superseded_by=None,
        )
        self.views.append(WorkflowView.model_validate_json(self.record.view))


class RemoteNodeGraph:
    """一个真实发 Agent HTTP 请求的远程节点，用于验证 runner 的操作边界。"""

    def __init__(self, state: WorkflowState, received: list[str | None]) -> None:
        self.state = state
        self.next: tuple[str, ...] = ("collect_core_report",)

        def handler(request: httpx.Request) -> httpx.Response:
            received.append(request.headers.get("X-Keyword-Operation-ID"))
            return httpx.Response(200, json={"result": {"ok": True}})

        self.client = AgentClient(
            "http://agent",
            token="agent-token-0123456789abcdef-0123456789",
            transport=httpx.MockTransport(handler),
        )

    def update_state(
        self,
        config: RunnableConfig,
        values: dict[str, object],
        **kwargs: object,
    ) -> None:
        assert kwargs == {}
        operation_ids = cast(dict[str, str], values["browser_operation_ids"])
        self.state["browser_operation_ids"].update(operation_ids)

    def stream(
        self, value: object, config: RunnableConfig, **kwargs: object
    ) -> Iterator[dict[str, dict[str, int]]]:
        assert kwargs == {"stream_mode": "updates", "durability": "sync"}
        self.state["browser_operation_ids"][
            "collect_core_report"
        ] = "operation-remote-1"
        self.client.call(
            "GET", "/v1/status", operation_id="operation-remote-1"
        )
        self.state["revision"] = 1
        self.state["aba_report_id"] = "aba-report-123"
        self.state["node_status"]["collect_core_report"] = {
            "status": "done",
            "detail": "采集完成",
            "updated_at": "2026-11-02T12:00:00+00:00",
        }
        self.next = ()
        yield {"collect_core_report": {"revision": 1}}

    def get_state(self, config: RunnableConfig) -> SimpleNamespace:
        return SimpleNamespace(values=self.state, next=self.next, tasks=())


class RecordingOperationStore:
    def __init__(self, workflow_store: RecordingStore) -> None:
        self.workflow_store = workflow_store
        self.succeeded: list[tuple[str, str]] = []

    def begin(self, **payload: object) -> BrowserOperation:
        return BrowserOperation(
            operation_id="operation-remote-1",
            workflow_id=str(payload["workflow_id"]),
            run_id=str(payload["run_id"]),
            node_key=str(payload["node_key"]),
            source=str(payload["source"]),
            idempotency_key=str(payload["idempotency_key"]),
            status="running",
            attempts=1,
            auto_resume_remaining=1,
            request_summary="{}",
            result_ref="",
            error_code="",
            created_at="now",
            updated_at="now",
        )

    def succeed(self, operation_id: str, *, result_ref: str) -> None:
        persisted = cast(dict[str, object], json.loads(self.workflow_store.record.state))
        assert cast(dict[str, str], persisted["browser_operation_ids"])[
            "collect_core_report"
        ] == operation_id
        assert cast(dict[str, dict[str, str]], persisted["node_status"])[
            "collect_core_report"
        ]["status"] == "done"
        self.succeeded.append((operation_id, result_ref))

    def wait(self, operation_id: str, *, error_code: str) -> None:
        raise AssertionError(f"unexpected wait: {operation_id} {error_code}")

    def fail(self, operation_id: str, *, error_code: str) -> None:
        raise AssertionError(f"unexpected failure: {operation_id} {error_code}")


def test_runner_wraps_remote_node_in_durable_operation_context() -> None:
    """抓住远程节点未建操作记录，或业务结果持久化前就提前标记成功。"""
    state = initial_state(workflow_id="w1", run_id="r1", project_id="p1", options={})
    stamp = "2026-11-02T11:59:00+00:00"
    queued = WorkflowView(
        workflow_id="w1",
        run_id="r1",
        project_id="p1",
        status="queued",
        revision=0,
        nodes=[
            WorkflowNodeView(key=key, label=NODE_LABELS[key], status="pending")
            for key in NODE_KEYS
        ],
        created_at=datetime.fromisoformat(stamp),
        updated_at=datetime.fromisoformat(stamp),
    )
    record = WorkflowRecord(
        workflow_id="w1",
        run_id="r1",
        project_id="p1",
        status="queued",
        revision=0,
        state=json.dumps(state),
        view=queued.model_dump_json(),
        created_at=stamp,
        updated_at=stamp,
        superseded_by=None,
    )
    received: list[str | None] = []
    workflow_store = RecordingStore(record)
    operation_store = RecordingOperationStore(workflow_store)
    runner = WorkflowRunner(
        cast(CompiledWorkflow, RemoteNodeGraph(state, received)),
        cast(WorkflowStore, workflow_store),
        operation_store=cast(object, operation_store),
        inline=True,
    )
    try:
        runner.start(record, state)
    finally:
        runner.close()

    assert received == ["operation-remote-1"]
    assert operation_store.succeeded == [
        ("operation-remote-1", "aba-report-123")
    ]


def test_runner_persists_each_streamed_node_before_next_finishes() -> None:
    """抓住 runner 只在整张图完成后保存一次、页面无法动态观察的回归。"""
    state = initial_state(workflow_id="w1", run_id="r1", project_id="p1", options={})
    stamp = "2026-11-01T12:00:00+00:00"
    stamp_datetime = datetime.fromisoformat(stamp)
    queued = WorkflowView(
        workflow_id="w1",
        run_id="r1",
        project_id="p1",
        status="queued",
        revision=0,
        nodes=[
            WorkflowNodeView(key=key, label=NODE_LABELS[key], status="pending") for key in NODE_KEYS
        ],
        created_at=stamp_datetime,
        updated_at=stamp_datetime,
    )
    record = WorkflowRecord(
        workflow_id="w1",
        run_id="r1",
        project_id="p1",
        status="queued",
        revision=0,
        state=json.dumps(state),
        view=queued.model_dump_json(),
        created_at=stamp,
        updated_at=stamp,
        superseded_by=None,
    )
    store = RecordingStore(record)
    runner = WorkflowRunner(
        cast(CompiledWorkflow, StreamingGraph(state)), cast(WorkflowStore, store), inline=True
    )
    try:
        runner.start(record, state)
    finally:
        runner.close()

    assert len(store.views) >= 3
    first_running = next(view for view in store.views if view.events)
    assert first_running.current_node == "ensure_input"
    assert first_running.events[-1].status == "running"
    between_nodes = next(
        view for view in store.views if len(view.events) == 2 and view.events[0].status == "done"
    )
    assert between_nodes.current_node == "identify_product"
    assert [(event.node_key, event.status) for event in between_nodes.events] == [
        ("ensure_input", "done"),
        ("identify_product", "running"),
    ]
    assert between_nodes.events[0].output_summary["结果"] == "输入正常，共 2 张图片"
    final = store.views[-1]
    assert final.status == "completed"
    assert [(event.node_key, event.status) for event in final.events] == [
        ("ensure_input", "done"),
        ("identify_product", "done"),
    ]


def test_runner_failure_closes_event_and_node_status() -> None:
    """抓住事件显示失败、节点胶囊却仍显示运行中的冲突。"""
    state = initial_state(workflow_id="w1", run_id="r1", project_id="p1", options={})
    stamp = "2026-11-01T12:00:00+00:00"
    queued = WorkflowView(
        workflow_id="w1",
        run_id="r1",
        project_id="p1",
        status="queued",
        revision=0,
        nodes=[
            WorkflowNodeView(key=key, label=NODE_LABELS[key], status="pending")
            for key in NODE_KEYS
        ],
        created_at=datetime.fromisoformat(stamp),
        updated_at=datetime.fromisoformat(stamp),
    )
    record = WorkflowRecord(
        workflow_id="w1",
        run_id="r1",
        project_id="p1",
        status="queued",
        revision=0,
        state=json.dumps(state),
        view=queued.model_dump_json(),
        created_at=stamp,
        updated_at=stamp,
        superseded_by=None,
    )
    store = RecordingStore(record)
    runner = WorkflowRunner(
        cast(CompiledWorkflow, FailingGraph(state)), cast(WorkflowStore, store), inline=True
    )
    try:
        runner.start(record, state)
    finally:
        runner.close()

    failed = store.views[-1]
    assert failed.status == "failed"
    assert failed.events[-1].status == "failed"
    assert failed.nodes[0].status == "failed"


class ControlledSaver(InMemorySaver):
    def __init__(self, *, fail_queue_write: bool = False) -> None:
        super().__init__()  # pyright: ignore[reportUnknownMemberType]
        self.fail_queue_write = fail_queue_write
        self.queue_write_started = Event()
        self.release_queue_write = Event()
        self.queue_persisted = Event()

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        first_queue = (
            checkpoint["channel_values"].get("hot_search_core_analysis_id")
            and not self.queue_write_started.is_set()
        )
        if first_queue:
            self.queue_write_started.set()
            if not self.release_queue_write.wait(10):
                raise TimeoutError("test did not release queued checkpoint write")
            if self.fail_queue_write:
                raise OSError("queued checkpoint disk write failed")
        saved = super().put(config, checkpoint, metadata, new_versions)
        if first_queue:
            self.queue_persisted.set()
        return saved


@pytest.mark.parametrize("fail_queue_write", [False, True])
def test_execute_waits_for_successful_queue_checkpoint(
    db: str,
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail_queue_write: bool,
) -> None:
    # Catches async durability starting paid execution before an ID is recoverably persisted.
    deps, state, _ = hot_workflow(db, engine, tmp_path, ["shoe storage"])
    saver = ControlledSaver(fail_queue_write=fail_queue_write)
    execute_started = Event()
    execution_saw_persisted: list[bool] = []
    original_execute = deps.hot_search_core_analysis.execute

    def execute(project: UUID, run: UUID, analysis_id: UUID) -> HotSearchCoreAnalysis:
        execution_saw_persisted.append(saver.queue_persisted.is_set())
        execute_started.set()
        return original_execute(project, run, analysis_id)

    monkeypatch.setattr(deps.hot_search_core_analysis, "execute", execute)
    nodes = build_nodes_1_2(deps)
    graph = StateGraph(WorkflowState)
    graph.add_node("collect_core_report", nodes["collect_core_report"])  # pyright: ignore[reportUnknownMemberType, reportArgumentType]
    graph.add_node("core_terms", nodes["core_terms"])  # pyright: ignore[reportUnknownMemberType, reportArgumentType]
    graph.add_edge(START, "collect_core_report")
    graph.add_edge("collect_core_report", "core_terms")
    graph.add_edge("core_terms", END)
    compiled = cast(CompiledWorkflow, graph.compile(checkpointer=saver))  # pyright: ignore[reportUnknownMemberType]
    store = WorkflowStore(engine)
    store.initialize()
    runner = WorkflowRunner(compiled, store)
    service = WorkflowService(deps, store, runner)
    project = UUID(state["project_id"])
    run = UUID(state["run_id"])
    try:
        view = service.start(project, run, WorkflowOptions(auto_decide=True))
        assert saver.queue_write_started.wait(5), "real queued checkpoint never reached saver"
        began_before_release = execute_started.wait(0.25)
        assert not began_before_release, (
            "paid execute started while queued checkpoint was unpersisted"
        )
    finally:
        saver.release_queue_write.set()
        runner.executor.submit(lambda: None).result(timeout=10)
        runner.close()
    latest = service.view(project, run, view.workflow_id)
    if fail_queue_write:
        assert latest.status == "failed"
        assert not saver.queue_persisted.is_set()
        assert not execute_started.is_set(), "failed persistence must prevent paid execution"
    else:
        assert latest.status == "completed", latest.error
        assert execution_saw_persisted == [True]
        snapshot = compiled.get_state({"configurable": {"thread_id": view.workflow_id}})
        assert snapshot.values["hot_search_core_analysis_id"]
        assert snapshot.values["core_selection_id"]
