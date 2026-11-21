"""编排用例：启动、查询、恢复、终止；负责项目隔离与并发校验。"""

import json
from datetime import datetime
from typing import cast
from uuid import UUID, uuid4

from app.domain.errors import DomainError
from app.domain.workflow import (
    ResumeRequest,
    WorkflowEventView,
    WorkflowNodeView,
    WorkflowOptions,
    WorkflowView,
)
from app.workflows.deps import WorkflowDeps
from app.workflows.events import interrupt_active_event
from app.workflows.runner import WorkflowRunner
from app.workflows.state import NODE_KEYS, NODE_LABELS, initial_state, now_iso
from app.workflows.store import WorkflowRecord, WorkflowStore
from app.workflows.view import pending

TERMINAL = frozenset({"completed", "failed", "superseded", "aborted", "incompatible"})


class WorkflowService:
    def __init__(self, deps: WorkflowDeps, store: WorkflowStore, runner: WorkflowRunner) -> None:
        self.deps = deps
        self.store = store
        self.runner = runner

    def start(
        self,
        project: UUID,
        run: UUID,
        options: WorkflowOptions,
        browser_device_id: str | None = None,
    ) -> WorkflowView:
        self.deps.repository.get_run(project, run)
        # 旧页面可能仍提交访问者设备。服务器模式始终清空该绑定。
        if self.deps.server_only:
            browser_device_id = None
        if browser_device_id and self.deps.devices is not None:
            self.deps.devices.get(browser_device_id)
        browser_device_id = self._resolve_device(browser_device_id)
        workflow_id = str(uuid4())
        state = initial_state(
            workflow_id=workflow_id,
            run_id=str(run),
            project_id=str(project),
            options=options.model_dump(mode="json"),
            browser_device_id=browser_device_id,
        )
        stamp = now_iso()
        queued = WorkflowView(
            workflow_id=workflow_id,
            run_id=str(run),
            project_id=str(project),
            status="queued",
            revision=0,
            nodes=[
                WorkflowNodeView(key=key, label=NODE_LABELS[key], status="pending")
                for key in NODE_KEYS
            ],
            next_poll_ms=1000,
            created_at=datetime.fromisoformat(stamp),
            updated_at=datetime.fromisoformat(stamp),
        )
        self.store.create(
            workflow_id=workflow_id,
            run_id=str(run),
            project_id=str(project),
            state=json.dumps(state, ensure_ascii=False),
            view=queued.model_dump_json(),
            created_at=stamp,
            exclusive=True,
        )
        self.store.mark_superseded(str(project), str(run), workflow_id, stamp)
        record = self.store.get(workflow_id)
        if record is None:
            raise DomainError(500, "WORKFLOW_START_FAILED", "编排实例创建失败")
        self.runner.start(record, state)
        return self.view(project, run, workflow_id)

    def _resolve_device(self, requested: str | None) -> str:
        """决定这次任务用哪台采集电脑。

        明确指定的设备始终保留，离线时等待它重连；只有未指定且唯一在线时自动选择。
        不能因为一台离线就把任务交给另一位访问者。
        """
        channel = self.deps.channel
        connected = channel.connected_devices() if channel is not None else []
        if requested:
            return requested
        if len(connected) == 1:
            device_id = connected[0]
            if self.deps.devices is not None:
                try:
                    self.deps.devices.get(device_id)
                except DomainError:
                    return requested or ""
            return device_id
        return requested or ""

    def view(self, project: UUID, run: UUID, workflow_id: str | None = None) -> WorkflowView:
        self.deps.repository.get_run(project, run)
        record = (
            self.store.get(workflow_id)
            if workflow_id
            else self.store.latest(str(project), str(run))
        )
        if record is None:
            raise DomainError(404, "WORKFLOW_NOT_FOUND", "尚未启动编排")
        if (record.project_id, record.run_id) != (str(project), str(run)):
            raise DomainError(404, "WORKFLOW_NOT_FOUND", "此任务下未找到该编排")
        return WorkflowView.model_validate_json(record.view)

    def history(self, project: UUID, run: UUID) -> list[WorkflowView]:
        self.deps.repository.get_run(project, run)
        return [
            WorkflowView.model_validate_json(record.view)
            for record in self.store.history(str(project), str(run))
        ]

    def _record(self, project: UUID, run: UUID, workflow_id: str) -> WorkflowRecord:
        self.deps.repository.get_run(project, run)
        record = self.store.get(workflow_id)
        if record is None or (record.project_id, record.run_id) != (str(project), str(run)):
            raise DomainError(404, "WORKFLOW_NOT_FOUND", "此任务下未找到该编排")
        return record

    def resume(
        self, project: UUID, run: UUID, workflow_id: str, request: ResumeRequest
    ) -> WorkflowView:
        record = self._record(project, run, workflow_id)
        if record.status == "superseded":
            raise DomainError(409, "WORKFLOW_SUPERSEDED", "该编排已被同任务的新实例取代")
        view = WorkflowView.model_validate_json(record.view)
        if record.status in ("interrupted", "failed") and view.waiting_for is None:
            if request.action == "abort":
                self._finish(record, "aborted")
                return self.view(project, run, workflow_id)
            if request.action != "continue":
                raise DomainError(409, "WORKFLOW_RECOVERY_INVALID", "中断工作流只能明确继续或终止")
            return self._resume_interrupted_core(project, run, record, request)
        if record.status in TERMINAL:
            raise DomainError(409, "WORKFLOW_FINISHED", "该编排已结束，请重新启动")
        if view.waiting_for is None:
            raise DomainError(409, "WORKFLOW_NOT_WAITING", "编排当前不在等待状态")
        if request.gate != view.waiting_for.gate:
            raise DomainError(
                409, "WORKFLOW_GATE_MISMATCH", "提交的门与当前等待的门不一致，请刷新后重试"
            )
        if request.action == "abort":
            self._finish(record, "aborted")
            return self.view(project, run, workflow_id)
        outcome = self.store.claim(workflow_id, request.expected_revision)
        if outcome == "missing":
            raise DomainError(404, "WORKFLOW_NOT_FOUND", "此任务下未找到该编排")
        if outcome == "state":
            raise DomainError(409, "WORKFLOW_NOT_WAITING", "编排已被其他操作推进，请刷新")
        if outcome == "revision":
            raise DomainError(409, "WORKFLOW_REVISION_CONFLICT", "状态已更新，请刷新后重试")
        self.runner.resume(record, request)
        return self.view(project, run, workflow_id)

    def _resume_interrupted_core(
        self, project: UUID, run: UUID, record: WorkflowRecord, request: ResumeRequest
    ) -> WorkflowView:
        config = self.runner._config(record.workflow_id)
        snapshot = self.runner.graph.get_state(config)
        state = snapshot.values
        if any(state.get(key) != value for key, value in (
            ("workflow_id", record.workflow_id),
            ("project_id", str(project)),
            ("run_id", str(run)),
        )):
            raise DomainError(409, "WORKFLOW_RECOVERY_CHECKPOINT_INVALID", "图检查点不属于当前任务")
        options = state.get("options")
        failed_node = state.get("failed_node")
        is_interrupted = record.status == "interrupted"
        is_failed_core = record.status == "failed" and failed_node == "core_terms"
        if (
            request.action != "continue"
            or request.gate != "core_terms"
            or not isinstance(options, dict)
            or options.get("reanalyze_core") is not True
            or not (is_interrupted or is_failed_core)
        ):
            raise DomainError(409, "WORKFLOW_RECOVERY_INVALID", "该中断点不是可恢复的核心词续跑")
        analysis_id = state.get("hot_search_core_analysis_id")
        report_id = state.get("aba_report_id")
        if not isinstance(analysis_id, str) or not isinstance(report_id, str):
            raise DomainError(409, "WORKFLOW_RECOVERY_EVIDENCE_MISSING", "核心词断点缺少分析或报告记录")
        paused, waiting = pending(snapshot)
        replay_failed_node = (
            record.status in ("interrupted", "failed")
            and failed_node == "core_terms"
            and not snapshot.next
        )
        expected_next = () if replay_failed_node else ("core_terms",)
        if snapshot.next != expected_next or waiting is not None or paused is not None:
            raise DomainError(409, "WORKFLOW_RECOVERY_CHECKPOINT_INVALID", "图检查点已变化，不能从核心词节点续跑")
        previous = self.deps.hot_search_core_analysis.get(project, run, UUID(analysis_id))
        active_analysis = self.deps.hot_search_core_analysis.get(project, run)
        if active_analysis.id != previous.id:
            raise DomainError(409, "WORKFLOW_RECOVERY_ANALYSIS_INVALID", "核心词分析已被新分析取代")
        queued_retry = previous.status == "queued" and previous.retry_of is not None
        if (
            previous.stale
            or previous.report_id != UUID(report_id)
            or (previous.status != "incomplete" and not queued_retry)
            or (queued_retry and any(batch.status in ("planned", "started", "outcome_unknown") for batch in previous.batches))
        ):
            raise DomainError(409, "WORKFLOW_RECOVERY_ANALYSIS_INVALID", "核心词分析已变化或不完整状态已改变")
        latest = self.store.latest(str(project), str(run))
        if latest is None or latest.workflow_id != record.workflow_id:
            raise DomainError(409, "WORKFLOW_SUPERSEDED", "该任务已被更新的自动运行取代")
        outcome = self.store.claim(
            record.workflow_id,
            request.expected_revision,
            retry_failed=record.status == "failed",
            resume_node="core_terms",
        )
        if outcome == "missing":
            raise DomainError(404, "WORKFLOW_NOT_FOUND", "此任务下未找到该编排")
        if outcome == "state":
            raise DomainError(409, "WORKFLOW_NOT_WAITING", "编排已被其他操作推进，请刷新")
        if outcome == "revision":
            raise DomainError(409, "WORKFLOW_REVISION_CONFLICT", "状态已更新，请刷新后重试")
        try:
            retry = previous if queued_retry else self.deps.hot_search_core_analysis.start(
                project,
                run,
                UUID(report_id),
                previous.own_brands_snapshot,
                retry_incomplete=True,
                retry_unknown_model_outcomes=True,
            )
            if not queued_retry and (retry.id == previous.id or retry.status != "queued" or retry.retry_of != previous.id):
                raise DomainError(409, "WORKFLOW_RECOVERY_RETRY_INVALID", "续跑分析没有继承原分析证据")
            self.runner.graph.update_state(
                config,
                {
                    "hot_search_core_analysis_id": str(retry.id),
                    "hot_search_core_analysis_status": "queued",
                    "core_analysis_id": str(retry.id),
                    "failed_node": None,
                    "error": "",
                    "redo_from": None,
                },
                as_node="collect_core_report" if replay_failed_node else None,
            )
        except Exception:
            current = self.store.get(record.workflow_id)
            if current is not None:
                self.store.save(
                    workflow_id=record.workflow_id,
                    status=record.status,
                    revision=current.revision,
                    state=record.state,
                    view=record.view,
                    now=now_iso(),
                )
            raise
        self.runner.resume(record, request)
        return self.view(project, run, record.workflow_id)

    def abort(self, project: UUID, run: UUID, workflow_id: str) -> WorkflowView:
        record = self._record(project, run, workflow_id)
        if record.status in TERMINAL:
            raise DomainError(409, "WORKFLOW_FINISHED", "该编排已结束")
        self._finish(record, "aborted")
        return self.view(project, run, workflow_id)

    def retry_traffic(
        self, project: UUID, run: UUID, workflow_id: str, request: ResumeRequest
    ) -> WorkflowView:
        record = self._record(project, run, workflow_id)
        state = json_loads(record.state)
        latest = self.store.latest(str(project), str(run))
        if (
            record.status != "failed"
            or state.get("failed_node") != "traffic_review"
            or latest is None
            or latest.workflow_id != workflow_id
            or request.gate != "traffic_review"
            or request.action != "continue"
        ):
            raise DomainError(409, "WORKFLOW_RETRY_INVALID", "仅可继续当前失败任务的流量审核")
        report = self.deps.traffic.get(project, run, UUID(str(state.get("traffic_id"))))
        if report.stale:
            raise DomainError(409, "TRAFFIC_INPUT_STALE", "清洗来源已变化，请先重新生成筛选")
        outcome = self.store.claim(workflow_id, request.expected_revision, retry_failed=True)
        if outcome != "ok":
            raise DomainError(409, "WORKFLOW_REVISION_CONFLICT", "任务已更新或正在执行，请刷新")
        self.runner.retry_traffic(record)
        return self.view(project, run, workflow_id)

    def retry_cleaning(
        self, project: UUID, run: UUID, workflow_id: str, request: ResumeRequest
    ) -> WorkflowView:
        record = self._record(project, run, workflow_id)
        state = json_loads(record.state)
        latest = self.store.latest(str(project), str(run))
        if (
            record.status != "failed"
            or state.get("failed_node") != "cleaning_review"
            or latest is None
            or latest.workflow_id != workflow_id
            or request.gate != "cleaning_review"
            or request.action != "continue"
        ):
            raise DomainError(409, "WORKFLOW_RETRY_INVALID", "仅可重试当前失败任务的清洗分类")
        cleaning_id = state.get("cleaning_id")
        if not isinstance(cleaning_id, str):
            raise DomainError(409, "WORKFLOW_CLEANING_MISSING", "尚无可重试的清洗快照")
        report = self.deps.cleaning.get(project, run, UUID(cleaning_id))
        progress = self.deps.cleaning.classification_progress(project, run, UUID(cleaning_id))
        # 部分失败也允许续跑：只补未完成的词，已成功的标签不会重发。
        if report.stale or (report.review is not None and progress.get("status") != "failed"):
            raise DomainError(409, "CLEANING_RETRY_INVALID", "清洗快照已过期或分类已完成")
        outcome = self.store.claim(workflow_id, request.expected_revision, retry_failed=True)
        if outcome != "ok":
            raise DomainError(409, "WORKFLOW_REVISION_CONFLICT", "任务已更新或正在执行，请刷新")
        self.runner.retry_cleaning(record)
        return self.view(project, run, workflow_id)

    def _finish(self, record: WorkflowRecord, status: str) -> None:
        payload = json_loads(record.view)
        payload["status"] = status
        payload["waiting_for"] = None
        payload["next_poll_ms"] = 0
        raw_events = payload.get("events", [])
        events: list[WorkflowEventView] = []
        if isinstance(raw_events, list):
            for item in cast(list[object], raw_events):
                try:
                    events.append(WorkflowEventView.model_validate(item))
                except ValueError:
                    continue
        reason = "用户终止了自动运行" if status == "aborted" else f"运行已结束：{status}"
        payload["events"] = [
            event.model_dump(mode="json") for event in interrupt_active_event(events, reason)
        ]
        # A terminal workflow must not expose running/waiting nodes in its view.
        # Otherwise the UI keeps spinning on a finished run after refresh.
        for node in payload.get("nodes", []):
            if isinstance(node, dict) and node.get("status") in (
                "running", "waiting_human", "waiting_external", "queued"
            ):
                node["status"] = "pending"
        payload["current_node"] = None
        payload["waiting_for"] = None
        self.store.save(
            workflow_id=record.workflow_id,
            status=status,
            revision=record.revision,
            state=record.state,
            view=json.dumps(payload, ensure_ascii=False),
            now=now_iso(),
        )


def json_loads(value: str) -> dict[str, object]:
    parsed = json.loads(value)
    return cast(dict[str, object], parsed) if isinstance(parsed, dict) else {}
