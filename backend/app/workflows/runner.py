"""按访问者设备执行与恢复图，并把结果写回状态投影。"""

import json
import logging
from collections.abc import Callable
from dataclasses import replace
from types import SimpleNamespace
from typing import Literal, cast

from app.adapters.browser_tasks import BrowserTaskExecutor
from app.adapters.remote_browser import browser_device_context
from app.domain.workflow import ResumeRequest, WaitingForView, WorkflowEventView, WorkflowStatus
from app.execution_control import (
    WorkflowCancelled,
    check_workflow_cancelled,
    workflow_execution_context,
)
from app.workflows.browser_operations import (
    REMOTE_NODE_SOURCES,
    BrowserOperationStore,
    browser_operation_identity,
)
from app.workflows.events import finish_node_event, start_node_event, wait_node_event
from app.workflows.protocols import CompiledWorkflow, SnapshotView
from app.workflows.state import NODE_KEYS, WorkflowState, now_iso
from app.workflows.store import WorkflowRecord, WorkflowStore
from app.workflows.view import build_view, pending
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command

logger = logging.getLogger(__name__)


def _stored_device_id(record: WorkflowRecord) -> str:
    """从已持久化的状态投影里读出该任务固定的设备 ID。"""
    try:
        payload = json.loads(record.state)
    except (TypeError, ValueError):
        return ""
    if not isinstance(payload, dict):
        return ""
    value = cast(dict[str, object], payload).get("browser_device_id")
    return value if isinstance(value, str) else ""


class WorkflowRunner:
    def __init__(
        self,
        graph: CompiledWorkflow,
        store: WorkflowStore,
        *,
        operation_store: BrowserOperationStore | None = None,
        inline: bool = False,
    ) -> None:
        self.graph = graph
        self.store = store
        self.operation_store = operation_store
        self.inline = inline
        # 同一设备保持有序，不同使用者的电脑可并行；事务与 claim 仍防止重复恢复。
        self.executor = BrowserTaskExecutor(thread_name_prefix="workflow")

    def close(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)

    def recover(self) -> None:
        """先从 checkpoint 恢复等待态，再中断确实没有 pending 的执行。"""
        for record in self.store.running():
            try:
                snapshot = self.graph.get_state(self._config(record.workflow_id))
                fallback = cast(WorkflowState, json.loads(record.state))
                state = self._state(snapshot, fallback)
                paused, waiting = pending(snapshot)
                if waiting is None:
                    continue
                events = self._waiting_events(
                    state,
                    paused or (snapshot.next[0] if snapshot.next else None),
                    waiting,
                    self._events(record),
                )
                self._persist(record, state, snapshot, events)
            except Exception:
                logger.exception("workflow checkpoint recovery failed %s", record.workflow_id)
        # 上面成功恢复的记录已不再是 queued/running；这里只处理没有可恢复等待点的实例。
        self.store.recover_running(now_iso())

    def start(self, record: WorkflowRecord, state: WorkflowState) -> None:
        if self.inline:
            self._invoke(record.workflow_id, state)
        else:
            self._submit(record, lambda: self._invoke(record.workflow_id, state))

    def resume(self, record: WorkflowRecord, request: ResumeRequest) -> None:
        # 节点只关心人提交的业务参数；门与版本校验在 service 层已完成。
        # resume 的输入交给暂停的 interrupt；节点会从入口重放，因此业务写入须可复用。
        value = Command(resume=request.payload)
        if self.inline:
            self._invoke(record.workflow_id, value)
        else:
            self._submit(record, lambda: self._invoke(record.workflow_id, value))

    def _submit(self, record: WorkflowRecord, task: Callable[[], None]) -> None:
        with browser_device_context(_stored_device_id(record)):
            self.executor.submit(task)

    def _config(self, workflow_id: str) -> RunnableConfig:
        return {"configurable": {"thread_id": workflow_id}}

    def retry_traffic(self, record: WorkflowRecord) -> None:
        if self.inline:
            self._retry_traffic(record)
        else:
            self._submit(record, lambda: self._retry_traffic(record))

    def retry_cleaning(self, record: WorkflowRecord) -> None:
        if self.inline:
            self._retry_cleaning(record)
        else:
            self._submit(record, lambda: self._retry_cleaning(record))

    def _retry_cleaning(self, record: WorkflowRecord) -> None:
        try:
            config = self._config(record.workflow_id)
            snapshot = self.graph.get_state(config)
            state = cast(WorkflowState, snapshot.values)
            self.graph.update_state(
                config,
                {
                    "failed_node": None,
                    "error": "",
                    "redo_from": None,
                    "options": {**state["options"], "retry_cleaning": True},
                },
                as_node="create_cleaning",
            )
        except Exception:
            logger.exception("workflow cleaning recovery failed %s", record.workflow_id)
            self._failed(record.workflow_id, "清洗自动分类恢复失败，请查看服务日志")
            return
        self._invoke(record.workflow_id, None)

    def _retry_traffic(self, record: WorkflowRecord) -> None:
        try:
            self.graph.update_state(
                self._config(record.workflow_id),
                {"failed_node": None, "error": "", "redo_from": None},
                as_node="create_traffic",
            )
        except Exception:
            logger.exception("workflow traffic recovery failed %s", record.workflow_id)
            self._failed(record.workflow_id, "流量审核恢复失败，请查看服务日志")
            return
        self._invoke(record.workflow_id, None)

    def _invoke(self, workflow_id: str, value: object) -> None:
        """整个任务共用一个访问者设备：连未被操作边界包裹的节点也不例外。"""
        record = self.store.get(workflow_id)
        device_id = _stored_device_id(record) if record is not None else ""
        def cancelled() -> bool:
            current = self.store.get(workflow_id)
            return current is None or current.status in {"aborted", "superseded"}

        with browser_device_context(device_id), workflow_execution_context(cancelled):
            try:
                check_workflow_cancelled()
                self._invoke_routed(workflow_id, value)
            except WorkflowCancelled:
                logger.info("workflow %s stopped at an operation boundary", workflow_id)

    def _invoke_routed(self, workflow_id: str, value: object) -> None:
        record = self.store.get(workflow_id)
        if record is None:
            return
        config = self._config(workflow_id)
        events = self._events(record)
        state: WorkflowState | None = None
        current: str | None = None
        try:
            snapshot = self.graph.get_state(config)
            state = self._state(snapshot, value)
            initial_input = isinstance(value, dict)
            current = (
                snapshot.next[0] if snapshot.next else (NODE_KEYS[0] if initial_input else None)
            )
            if current is not None:
                events = start_node_event(
                    events, current, input_data=self._input_summary(state, current)
                )
                record = self._persist(record, state, self._with_next(snapshot, current), events)

            # 同步 checkpoint 保证下一个节点开始前，前一节点状态已可恢复。
            stream = iter(
                self.graph.stream(value, config, stream_mode="updates", durability="sync")
            )
            while True:
                check_workflow_cancelled()
                try:
                    update = next(stream)
                except StopIteration:
                    break
                check_workflow_cancelled()
                snapshot = self.graph.get_state(config)
                state = cast(WorkflowState, snapshot.values)
                paused, waiting = pending(snapshot)
                updated_nodes = [
                    key for key in update if not key.startswith("__") and key in NODE_KEYS
                ]
                if waiting is not None:
                    waiting_node = paused or current
                    events = self._waiting_events(state, waiting_node, waiting, events)
                    completed_node = waiting_node
                    current = None
                else:
                    completed_node = updated_nodes[-1] if updated_nodes else None
                    for node in updated_nodes:
                        entry = state["node_status"].get(node)
                        raw_status = entry["status"] if entry is not None else "done"
                        event_status = cast(
                            Literal["done", "failed", "skipped"],
                            raw_status if raw_status in {"done", "failed", "skipped"} else "done",
                        )
                        events = finish_node_event(
                            events,
                            node,
                            status=event_status,
                            message=entry["detail"] if entry and entry["detail"] else "执行完成",
                            output_data=self._output_summary(state, node),
                            error=state["error"] if event_status == "failed" else "",
                        )
                    current = snapshot.next[0] if snapshot.next else None
                    if current is not None and not self._is_running(events, current):
                        events = start_node_event(
                            events,
                            current,
                            input_data=self._input_summary(state, current),
                        )
                record = self._persist(record, state, snapshot, events)
                if completed_node is not None and waiting is None:
                    self._finish_operation(state, completed_node, False)

            # LangGraph 的 interrupt 可能只存在于最终 checkpoint，不一定再产生一条 update。
            # 流耗尽后必须重新读取快照，避免投影永久停在 running。
            snapshot = self.graph.get_state(config)
            state = cast(WorkflowState, snapshot.values)
            paused, waiting = pending(snapshot)
            if waiting is not None and record.status not in {
                "waiting_external",
                "waiting_human",
            }:
                events = self._waiting_events(state, paused or current, waiting, events)
                self._persist(record, state, snapshot, events)
        except Exception:
            logger.exception("workflow %s failed", workflow_id)
            self._failed(workflow_id, "编排执行异常，请查看服务日志")
            if current is not None and state is not None:
                self._fail_operation(state, current, "WORKFLOW_EXECUTION_ERROR")
            return

    def _waiting_events(
        self,
        state: WorkflowState,
        waiting_node: str | None,
        waiting: WaitingForView,
        events: list[WorkflowEventView],
    ) -> list[WorkflowEventView]:
        if waiting_node is None:
            return events
        output_data = self._output_summary(state, waiting_node)
        output_data.update(self._wait_operation(state, waiting_node, waiting.refs))
        return wait_node_event(
            events,
            waiting_node,
            kind="external" if waiting.kind == "external" else "human",
            message=waiting.title,
            output_data=output_data,
        )

    @staticmethod
    def _result_ref(state: WorkflowState, node: str) -> str:
        refs: dict[str, object] = {
            "collect_core_report": state.get("aba_report_id"),
            "competitor_discovery": state.get("competitors_id"),
            "submit_sources": [
                state.get("expansion_forward_id"),
                state.get("expansion_reverse_id"),
            ],
            "wait_sources": [
                state.get("expansion_forward_id"),
                state.get("expansion_reverse_id"),
            ],
            "third_party": state.get("third_party_ids"),
        }
        value = refs.get(node)
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return json.dumps(
                [item for item in cast(list[object], value) if item], ensure_ascii=False
            )
        return ""

    def _finish_operation(self, state: WorkflowState, node: str, waiting: bool) -> None:
        if self.operation_store is None or node not in REMOTE_NODE_SOURCES:
            return
        operation_id = state["browser_operation_ids"].get(node)
        if operation_id is None:
            return
        if waiting:
            self.operation_store.wait(operation_id, error_code="WAITING_EXTERNAL")
            return
        entry = state["node_status"].get(node)
        if entry is not None and entry["status"] == "failed":
            self.operation_store.fail(operation_id, error_code="WORKFLOW_NODE_FAILED")
            return
        self.operation_store.succeed(operation_id, result_ref=self._result_ref(state, node))

    def _wait_operation(
        self, state: WorkflowState, node: str, refs: dict[str, str]
    ) -> dict[str, object]:
        if self.operation_store is None or node not in REMOTE_NODE_SOURCES:
            return {}
        source, idempotency_key, _summary = browser_operation_identity(state, node)
        operation = self.operation_store.get_by_key(
            run_id=state["run_id"],
            node_key=node,
            source=source,
            idempotency_key=idempotency_key,
        )
        if operation is None:
            return {}
        error_code = refs.get("error_code", "WAITING_EXTERNAL")
        allow_auto_resume = (
            refs.get("auto_resume") == "1" or refs.get("background_poll") == "1"
        ) and refs.get("attention_required") != "1"
        self.operation_store.wait(
            operation.operation_id,
            error_code=error_code,
            allow_auto_resume=allow_auto_resume,
        )
        return {
            "来源": source,
            "操作ID": operation.operation_id,
            "错误码": error_code,
        }

    def _fail_operation(self, state: WorkflowState, node: str, error_code: str) -> None:
        if self.operation_store is None or node not in REMOTE_NODE_SOURCES:
            return
        operation_id = state["browser_operation_ids"].get(node)
        if operation_id is not None:
            self.operation_store.fail(operation_id, error_code=error_code)

    @staticmethod
    def _with_next(snapshot: SnapshotView, node: str) -> SnapshotView:
        if snapshot.next:
            return snapshot
        return cast(
            SnapshotView,
            SimpleNamespace(values=snapshot.values, next=(node,), tasks=snapshot.tasks),
        )

    @staticmethod
    def _state(snapshot: SnapshotView, value: object) -> WorkflowState:
        values = snapshot.values
        if values.get("workflow_id"):
            return cast(WorkflowState, values)
        return cast(WorkflowState, value)

    @staticmethod
    def _events(record: WorkflowRecord) -> list[WorkflowEventView]:
        try:
            decoded = cast(object, json.loads(record.view))
            if not isinstance(decoded, dict):
                return []
            payload = cast(dict[str, object], decoded)
            raw = payload.get("events", [])
            if not isinstance(raw, list):
                return []
            return [WorkflowEventView.model_validate(item) for item in cast(list[object], raw)]
        except (TypeError, ValueError, json.JSONDecodeError):
            return []

    @staticmethod
    def _is_running(events: list[WorkflowEventView], node: str) -> bool:
        return any(event.node_key == node and event.status == "running" for event in events)

    @staticmethod
    def _artifacts(state: WorkflowState) -> dict[str, object]:
        keys = (
            "root_selection_id",
            "aba_report_id",
            "core_analysis_id",
            "core_selection_id",
            "competitors_id",
            "expansion_forward_id",
            "expansion_reverse_id",
            "third_party_ids",
            "cleaning_id",
            "traffic_id",
            "library_id",
            "publication_version",
        )
        return {key: state[key] for key in keys if state.get(key) not in (None, [], "")}

    def _input_summary(self, state: WorkflowState, node: str) -> dict[str, object]:
        return {
            "节点": node,
            "报告周": state.get("aba_week_end") or "未选择",
            "当前版本": state.get("revision", 0),
            "已有产物": self._artifacts(state),
        }

    def _output_summary(self, state: WorkflowState, node: str) -> dict[str, object]:
        entry = state["node_status"].get(node)
        return {
            "状态": entry["status"] if entry is not None else "done",
            "结果": entry["detail"] if entry is not None else "",
            "当前版本": state.get("revision", 0),
            "产物": self._artifacts(state),
        }

    def _persist(
        self,
        record: WorkflowRecord,
        state: WorkflowState,
        snapshot: SnapshotView,
        events: list[WorkflowEventView],
        override: WorkflowStatus | None = None,
    ) -> WorkflowRecord:
        check_workflow_cancelled()
        stamp = now_iso()
        current = replace(record, updated_at=stamp)
        view = build_view(current, state, snapshot, override, events)
        self.store.save(
            workflow_id=record.workflow_id,
            status=view.status,
            revision=view.revision,
            state=json.dumps(state, ensure_ascii=False),
            view=view.model_dump_json(),
            now=stamp,
        )
        return self.store.get(record.workflow_id) or replace(
            current,
            status=view.status,
            revision=view.revision,
            state=json.dumps(state, ensure_ascii=False),
            view=view.model_dump_json(),
        )

    def _failed(self, workflow_id: str, message: str) -> None:
        record = self.store.get(workflow_id)
        if record is None:
            return
        payload = cast(dict[str, object], json.loads(record.view))
        events = self._events(record)
        running_node = next(
            (event.node_key for event in reversed(events) if event.status == "running"), None
        )
        if running_node is not None:
            events = finish_node_event(
                events, running_node, status="failed", error=message, message="执行失败"
            )
        payload["status"] = "failed"
        payload["error"] = message
        payload["waiting_for"] = None
        payload["next_poll_ms"] = 0
        payload["events"] = [event.model_dump(mode="json") for event in events]
        raw_nodes = payload.get("nodes", [])
        if running_node is not None and isinstance(raw_nodes, list):
            for item in cast(list[object], raw_nodes):
                if not isinstance(item, dict):
                    continue
                node = cast(dict[str, object], item)
                if node.get("key") == running_node:
                    node["status"] = "failed"
                    node["detail"] = message
                    break
        self.store.save(
            workflow_id=record.workflow_id,
            status="failed",
            revision=record.revision,
            state=record.state,
            view=json.dumps(payload, ensure_ascii=False),
            now=now_iso(),
        )
