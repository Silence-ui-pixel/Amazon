"""PostgreSQL store for durable and idempotent remote browser operations."""

from __future__ import annotations

import json
from collections.abc import Callable
from functools import wraps
from hashlib import sha256
from typing import cast
from uuid import uuid4

from sqlalchemy import Engine

from app.adapters.remote_browser import browser_device_context, browser_operation_context
from app.db.scope import ConnectionScope
from app.domain.browser_operations import BrowserOperation
from app.workflows.events import summarize
from app.workflows.state import WorkflowState, now_iso

REMOTE_NODE_SOURCES: dict[str, str] = {
    "root_selection": "amazon_product_term",
    "collect_core_report": "aba",
    "competitor_discovery": "amazon_poe",
    "submit_sources": "aba_expansion",
    "wait_sources": "aba_expansion",
    "third_party": "sellersprite",
}

OperationNode = Callable[[WorkflowState], dict[str, object]]


def browser_operation_identity(
    state: WorkflowState, node_key: str
) -> tuple[str, str, dict[str, object]]:
    artifact_keys = (
        "root_selection_id",
        "aba_report_id",
        "core_analysis_id",
        "core_selection_id",
        "competitors_id",
        "expansion_forward_id",
        "expansion_reverse_id",
        "third_party_ids",
    )
    summary: dict[str, object] = {
        "节点": node_key,
        "报告周": state.get("aba_week_end") or "未选择",
        "输入产物": {
            key: state[key] for key in artifact_keys if state.get(key) not in (None, [], "")
        },
    }
    encoded = json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return REMOTE_NODE_SOURCES[node_key], sha256(encoded.encode("utf-8")).hexdigest(), summary


def wrap_browser_operation(
    node_key: str, node: OperationNode, store: BrowserOperationStore
) -> OperationNode:
    """在 LangGraph 节点内部建立操作边界，避免运行中外部修改 checkpoint。"""
    @wraps(node)
    def wrapped(state: WorkflowState) -> dict[str, object]:
        # Legacy/in-memory checkpoints can be replayed without a workflow_runs
        # projection. Such executions cannot be resumed by the scheduler, so do
        # not manufacture an orphan browser operation for them.
        if not store.has_workflow(state["workflow_id"]):
            return node(state)
        source, idempotency_key, summary = browser_operation_identity(state, node_key)
        operation = store.begin(
            workflow_id=state["workflow_id"],
            run_id=state["run_id"],
            node_key=node_key,
            source=source,
            idempotency_key=idempotency_key,
            request_summary=summary,
        )
        with (
            browser_operation_context(operation.operation_id),
            browser_device_context(state.get("browser_device_id")),
        ):
            result = node(state)
        return {
            **result,
            "browser_operation_ids": {node_key: operation.operation_id},
        }

    return wrapped


class BrowserOperationStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def _connect(self) -> ConnectionScope:
        return ConnectionScope(self.engine)

    def has_workflow(self, workflow_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM workflow_runs WHERE workflow_id=?", (workflow_id,)
            ).fetchone()
        return row is not None

    @staticmethod
    def _row(row: tuple[object, ...]) -> BrowserOperation:
        return BrowserOperation(
            operation_id=cast(str, row[0]),
            workflow_id=cast(str, row[1]),
            run_id=cast(str, row[2]),
            node_key=cast(str, row[3]),
            source=cast(str, row[4]),
            idempotency_key=cast(str, row[5]),
            status=cast(str, row[6]),
            attempts=cast(int, row[7]),
            auto_resume_remaining=cast(int, row[8]),
            request_summary=cast(str, row[9]),
            result_ref=cast(str, row[10]),
            error_code=cast(str, row[11]),
            created_at=cast(str, row[12]),
            updated_at=cast(str, row[13]),
        )

    @staticmethod
    def _select() -> str:
        return (
            "SELECT operation_id,workflow_id,run_id,node_key,source,idempotency_key,"
            "status,attempts,auto_resume_remaining,request_summary,result_ref,error_code,"
            "created_at,updated_at FROM browser_operations "
        )

    def begin(
        self,
        *,
        workflow_id: str,
        run_id: str,
        node_key: str,
        source: str,
        idempotency_key: str,
        request_summary: dict[str, object],
    ) -> BrowserOperation:
        operation_id = str(uuid4())
        stamp = now_iso()
        safe_summary = json.dumps(summarize(request_summary), ensure_ascii=False)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO browser_operations "
                "(operation_id,workflow_id,run_id,node_key,source,idempotency_key,status,"
                "attempts,auto_resume_remaining,request_summary,result_ref,error_code,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT (run_id,node_key,source,idempotency_key) DO NOTHING",
                (
                    operation_id,
                    workflow_id,
                    run_id,
                    node_key,
                    source,
                    idempotency_key,
                    "running",
                    1,
                    1,
                    safe_summary,
                    "",
                    "",
                    stamp,
                    stamp,
                ),
            )
            row = connection.execute(
                self._select()
                + "WHERE run_id=? AND node_key=? AND source=? AND idempotency_key=?",
                (run_id, node_key, source, idempotency_key),
            ).fetchone()
        assert row is not None
        return self._row(cast(tuple[object, ...], row))

    def get(self, operation_id: str) -> BrowserOperation | None:
        with self._connect() as connection:
            row = connection.execute(
                self._select() + "WHERE operation_id=?", (operation_id,)
            ).fetchone()
        return self._row(cast(tuple[object, ...], row)) if row else None

    def get_by_key(
        self,
        *,
        run_id: str,
        node_key: str,
        source: str,
        idempotency_key: str,
    ) -> BrowserOperation | None:
        with self._connect() as connection:
            row = connection.execute(
                self._select()
                + "WHERE run_id=? AND node_key=? AND source=? AND idempotency_key=?",
                (run_id, node_key, source, idempotency_key),
            ).fetchone()
        return self._row(cast(tuple[object, ...], row)) if row else None

    def succeed(self, operation_id: str, *, result_ref: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE browser_operations SET status='succeeded',result_ref=?,error_code='',"
                "updated_at=? WHERE operation_id=? AND status<>'succeeded'",
                (result_ref, now_iso(), operation_id),
            )

    def wait(
        self, operation_id: str, *, error_code: str, allow_auto_resume: bool = True
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE browser_operations SET status='waiting',error_code=?,"
                "auto_resume_remaining=CASE WHEN ? THEN auto_resume_remaining ELSE 0 END,"
                "updated_at=? "
                "WHERE operation_id=? AND status<>'succeeded'",
                (error_code, allow_auto_resume, now_iso(), operation_id),
            )

    def fail(self, operation_id: str, *, error_code: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE browser_operations SET status='failed',error_code=?,updated_at=? "
                "WHERE operation_id=? AND status<>'succeeded'",
                (error_code, now_iso(), operation_id),
            )

    def consume_auto_resume(self, operation_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "UPDATE browser_operations SET status='running',attempts=attempts+1,"
                "auto_resume_remaining=auto_resume_remaining-1,updated_at=? "
                "WHERE operation_id=? AND status='waiting' AND auto_resume_remaining>0 "
                "RETURNING operation_id",
                (now_iso(), operation_id),
            ).fetchone()
        return row is not None

    def waiting(self, limit: int = 20) -> list[BrowserOperation]:
        with self._connect() as connection:
            rows = connection.execute(
                self._select()
                + "WHERE status='waiting' AND auto_resume_remaining>0 "
                "ORDER BY updated_at,operation_id LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row(cast(tuple[object, ...], row)) for row in rows]
