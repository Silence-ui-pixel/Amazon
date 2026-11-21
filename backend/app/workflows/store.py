"""编排实例注册表与状态投影；checkpoint 是权威状态，这里只做检索与抢占。

``workflow_runs`` 表与业务库同库同 schema，schema 由 Alembic 迁移创建。
写入用短事务；claim() 用 ``SELECT ... FOR UPDATE`` 做「读状态 + 置 running」的
原子抢占，防止同一实例被并发 resume。
"""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast

from app.db.scope import ConnectionScope
from app.domain.errors import DomainError
from app.domain.workflow import WorkflowEventView
from app.workflows.events import interrupt_active_event
from sqlalchemy import Engine

ClaimOutcome = Literal["ok", "missing", "state", "revision"]


@dataclass(frozen=True)
class WorkflowRecord:
    workflow_id: str
    run_id: str
    project_id: str
    status: str
    revision: int
    state: str
    view: str
    created_at: str
    updated_at: str
    superseded_by: str | None


class WorkflowStore:
    """工作流投影表；与业务库共享同一个 SQLAlchemy 引擎。"""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def initialize(self) -> None:
        """schema 由 Alembic 迁移创建；此处仅校验连通性。"""
        with self._connect() as conn:
            conn.execute("SELECT 1")

    def _connect(self) -> ConnectionScope:
        return ConnectionScope(self.engine)

    def _row(self, row: tuple[object, ...]) -> WorkflowRecord:
        return WorkflowRecord(
            workflow_id=cast(str, row[0]),
            run_id=cast(str, row[1]),
            project_id=cast(str, row[2]),
            status=cast(str, row[3]),
            revision=cast(int, row[4]),
            state=cast(str, row[5]),
            view=cast(str, row[6]),
            created_at=cast(str, row[7]),
            updated_at=cast(str, row[8]),
            superseded_by=cast(str | None, row[9]),
        )

    def create(
        self,
        *,
        workflow_id: str,
        run_id: str,
        project_id: str,
        state: str,
        view: str,
        created_at: str,
        exclusive: bool = False,
    ) -> None:
        with self._connect() as conn:
            if exclusive:
                # 对同一 project+run 的并发创建做会话级串行化，替代 SQLite 的全局写锁。
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(?))", (f"{project_id}|{run_id}",)
                )
                active = conn.execute(
                    "SELECT 1 FROM workflow_runs WHERE project_id=? AND run_id=? "
                    "AND status IN ('queued','running') LIMIT 1",
                    (project_id, run_id),
                ).fetchone()
                if active:
                    raise DomainError(
                        409, "WORKFLOW_ALREADY_RUNNING", "该任务已有自动流程执行中，请等待完成"
                    )
            conn.execute(
                "INSERT INTO workflow_runs "
                "(workflow_id, run_id, project_id, status, revision, state, view, "
                "created_at, updated_at, superseded_by) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    workflow_id,
                    run_id,
                    project_id,
                    "queued",
                    0,
                    state,
                    view,
                    created_at,
                    created_at,
                    None,
                ),
            )

    def get(self, workflow_id: str) -> WorkflowRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT workflow_id,run_id,project_id,status,revision,state,view,"
                "created_at,updated_at,superseded_by FROM workflow_runs WHERE workflow_id=?",
                (workflow_id,),
            ).fetchone()
        return self._row(cast(tuple[object, ...], row)) if row else None

    def latest(self, project_id: str, run_id: str) -> WorkflowRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT workflow_id,run_id,project_id,status,revision,state,view,"
                "created_at,updated_at,superseded_by FROM workflow_runs "
                "WHERE project_id=? AND run_id=? ORDER BY created_at DESC, seq DESC LIMIT 1",
                (project_id, run_id),
            ).fetchone()
        return self._row(cast(tuple[object, ...], row)) if row else None

    def waiting_external(self, limit: int = 20) -> list[WorkflowRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT workflow_id,run_id,project_id,status,revision,state,view,"
                "created_at,updated_at,superseded_by FROM workflow_runs "
                "WHERE status='waiting_external' ORDER BY updated_at,seq LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row(cast(tuple[object, ...], row)) for row in rows]

    def running(self) -> list[WorkflowRecord]:
        """返回需要在启动时与 LangGraph checkpoint 对账的执行中实例。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT workflow_id,run_id,project_id,status,revision,state,view,"
                "created_at,updated_at,superseded_by FROM workflow_runs "
                "WHERE status IN ('queued','running') ORDER BY updated_at,seq"
            ).fetchall()
        return [self._row(cast(tuple[object, ...], row)) for row in rows]

    def history(self, project_id: str, run_id: str, limit: int = 50) -> list[WorkflowRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT workflow_id,run_id,project_id,status,revision,state,view,"
                "created_at,updated_at,superseded_by FROM workflow_runs "
                "WHERE project_id=? AND run_id=? ORDER BY created_at DESC, seq DESC LIMIT ?",
                (project_id, run_id, limit),
            ).fetchall()
        return [self._row(cast(tuple[object, ...], row)) for row in rows]

    def save(
        self,
        *,
        workflow_id: str,
        status: str,
        revision: int,
        state: str,
        view: str,
        now: str,
        superseded_by: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE workflow_runs SET status=?, revision=?, state=?, view=?, updated_at=?, "
                "superseded_by=? WHERE workflow_id=? "
                "AND (status NOT IN ('aborted','superseded') OR status=?)",
                (status, revision, state, view, now, superseded_by, workflow_id, status),
            )

    def claim(
        self, workflow_id: str, expected_revision: int, *, retry_failed: bool = False,
        resume_node: str | None = None,
    ) -> ClaimOutcome:
        """把实例从等待态原子置为 running；状态或版本不符时不动。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status, revision FROM workflow_runs WHERE workflow_id=? FOR UPDATE",
                (workflow_id,),
            ).fetchone()
            if row is None:
                return "missing"
            status = str(row[0])
            revision = int(row[1])
            allowed = (
                ("failed",)
                if retry_failed
                else ("queued", "waiting_human", "waiting_external", "interrupted")
            )
            if status not in allowed:
                return "state"
            if retry_failed:
                sibling = conn.execute(
                    "SELECT 1 FROM workflow_runs a JOIN workflow_runs b "
                    "ON a.project_id=b.project_id AND a.run_id=b.run_id "
                    "WHERE a.workflow_id=? AND b.workflow_id<>a.workflow_id "
                    "AND b.status IN ('queued','running')",
                    (workflow_id,),
                ).fetchone()
                if sibling:
                    return "state"
            if revision != expected_revision:
                return "revision"
            conn.execute(
                "UPDATE workflow_runs SET status='running' WHERE workflow_id=?", (workflow_id,)
            )
            if retry_failed or resume_node is not None:
                raw_view_row = conn.execute(
                    "SELECT view FROM workflow_runs WHERE workflow_id=?", (workflow_id,)
                ).fetchone()
                assert raw_view_row is not None
                raw_view = cast(str, raw_view_row[0])
                view = json.loads(raw_view)
                view.update(
                    status="running", error="", next_poll_ms=1500, waiting_for=None,
                )
                if resume_node is not None:
                    view["current_node"] = resume_node
                conn.execute(
                    "UPDATE workflow_runs SET view=? WHERE workflow_id=?",
                    (json.dumps(view, ensure_ascii=False), workflow_id),
                )
        return "ok"

    def mark_superseded(self, project_id: str, run_id: str, keep_id: str, now: str) -> None:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT workflow_id, state, view, revision FROM workflow_runs "
                "WHERE project_id=? AND run_id=? AND workflow_id<>? "
                "AND status IN ('queued','running','waiting_human','waiting_external','interrupted')",
                (project_id, run_id, keep_id),
            ).fetchall()
        for raw in rows:
            workflow_id, state, view, revision = cast(tuple[str, str, str, int], raw)
            decoded = cast(object, json.loads(view))
            if not isinstance(decoded, dict):
                continue
            payload = cast(dict[str, object], decoded)
            payload["status"] = "superseded"
            payload["waiting_for"] = None
            payload["next_poll_ms"] = 0
            payload["events"] = [
                event.model_dump(mode="json")
                for event in interrupt_active_event(
                    self._view_events(payload),
                    "已被新的自动运行取代",
                    now=datetime.fromisoformat(now),
                )
            ]
            self.save(
                workflow_id=workflow_id,
                status="superseded",
                revision=revision,
                state=state,
                view=json.dumps(payload, ensure_ascii=False),
                now=now,
                superseded_by=keep_id,
            )

    def recover_running(self, now: str) -> list[str]:
        """进程重启后，把未完成的实例标为 interrupted，不自动重跑。"""
        recovered: list[str] = []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT workflow_id, state, view FROM workflow_runs "
                "WHERE status IN ('queued','running')"
            ).fetchall()
        for raw in rows:
            workflow_id, state, view = cast(tuple[str, str, str], raw)
            payload = cast(dict[str, object], json.loads(view))
            payload["status"] = "interrupted"
            payload["next_poll_ms"] = 0
            payload["events"] = [
                event.model_dump(mode="json")
                for event in interrupt_active_event(
                    self._view_events(payload),
                    "服务重启，运行已中断",
                    now=datetime.fromisoformat(now),
                )
            ]
            record = self.get(workflow_id)
            if record is None:
                continue
            self.save(
                workflow_id=workflow_id,
                status="interrupted",
                revision=record.revision,
                state=state,
                view=json.dumps(payload, ensure_ascii=False),
                now=now,
            )
            recovered.append(workflow_id)
        return recovered

    @staticmethod
    def _view_events(payload: dict[str, object]) -> list[WorkflowEventView]:
        raw = payload.get("events", [])
        if not isinstance(raw, list):
            return []
        result: list[WorkflowEventView] = []
        for item in cast(list[object], raw):
            try:
                result.append(WorkflowEventView.model_validate(item))
            except ValueError:
                continue
        return result
