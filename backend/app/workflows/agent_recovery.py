"""后台探测紫鸟采集端，并对持久等待实例执行一次有限恢复。"""

import json
from collections.abc import Callable
from threading import Event, Thread
from typing import Protocol, cast

from app.adapters.remote_browser import browser_device_context
from app.domain.workflow import ResumeRequest, WorkflowView
from app.workflows.browser_operations import BrowserOperationStore
from app.workflows.store import WorkflowRecord, WorkflowStore


class ResumeRunner(Protocol):
    def resume(self, record: WorkflowRecord, request: ResumeRequest) -> None: ...


class AgentRecoveryScheduler:
    def __init__(
        self,
        workflows: WorkflowStore,
        operations: BrowserOperationStore,
        runner: ResumeRunner,
        probe: Callable[[], str],
        *,
        interval_seconds: float = 10.0,
    ) -> None:
        self.workflows = workflows
        self.operations = operations
        self.runner = runner
        self.probe = probe
        self.interval_seconds = interval_seconds
        self._stop = Event()
        self._thread: Thread | None = None

    def tick(self) -> int:
        """恢复至多一个实例；离线、忙碌与人工处理状态都不消费额度。"""
        for record in self.workflows.waiting_external():
            view = WorkflowView.model_validate_json(record.view)
            waiting = view.waiting_for
            if waiting is None or waiting.refs.get("background_poll") != "1":
                continue
            if self.workflows.claim(record.workflow_id, record.revision) != "ok":
                continue
            self.runner.resume(
                record,
                ResumeRequest(
                    gate=waiting.gate,
                    action="poll",
                    expected_revision=record.revision,
                    payload={},
                ),
            )
            return 1
        # 采集端掉线时节点还没创建 browser_operation，因此上面两段都扫不到；
        # 这里按 auto_resume 标记补一遍，采集端恢复后不必等用户回到页面点继续。
        for record in self.workflows.waiting_external():
            view = WorkflowView.model_validate_json(record.view)
            waiting = view.waiting_for
            if waiting is None or waiting.refs.get("background_poll") == "1":
                continue
            if waiting.refs.get("auto_resume") != "1" or waiting.refs.get("attention_required") == "1":
                continue
            try:
                stored: object = json.loads(record.state)
            except (ValueError, TypeError):
                continue
            if not isinstance(stored, dict):
                continue
            device = cast(dict[str, object], stored).get("browser_device_id")
            with browser_device_context(device if isinstance(device, str) else None):
                if self.probe() != "online":
                    continue
            if self.workflows.claim(record.workflow_id, record.revision) != "ok":
                continue
            self.runner.resume(
                record,
                ResumeRequest(
                    gate=waiting.gate,
                    action="poll",
                    expected_revision=record.revision,
                    payload={},
                ),
            )
            return 1
        for operation in self.operations.waiting():
            record = self.workflows.get(operation.workflow_id)
            if record is None:
                continue
            view = WorkflowView.model_validate_json(record.view)
            waiting = view.waiting_for
            if waiting is None or waiting.refs.get("attention_required") == "1":
                continue
            if waiting.refs.get("background_poll") == "1":
                continue
            try:
                stored: object = json.loads(record.state)
            except (ValueError, TypeError):
                continue
            if not isinstance(stored, dict):
                continue
            device = cast(dict[str, object], stored).get("browser_device_id")
            with browser_device_context(device if isinstance(device, str) else None):
                if self.probe() != "online":
                    continue
            if self.workflows.claim(record.workflow_id, record.revision) != "ok":
                continue
            if not self.operations.consume_auto_resume(operation.operation_id):
                continue
            self.runner.resume(
                record,
                ResumeRequest(
                    gate=waiting.gate,
                    action="poll",
                    expected_revision=record.revision,
                    payload={},
                ),
            )
            return 1
        return 0

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self.tick()
            except Exception:
                # 后台探测不得杀死 API 进程；下一周期继续，详细错误由服务日志记录。
                continue

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = Thread(target=self._loop, name="agent-recovery", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.interval_seconds + 1.0))
