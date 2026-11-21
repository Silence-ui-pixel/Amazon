from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from threading import Event, Lock, Thread

import pytest
from fastapi.testclient import TestClient

from agent.main import create_app
from agent.runtime import AgentRuntime
from app.domain.browser_agent import BrowserAgentStatus
from app.domain.errors import DomainError

TOKEN = "agent-token-0123456789abcdef-0123456789"


def _runtime(tmp_path: Path) -> AgentRuntime:
    runtime = object.__new__(AgentRuntime)
    runtime.data = tmp_path
    runtime.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="agent-status-test")
    runtime.lock = Lock()
    runtime.sessions = {}
    runtime._status = BrowserAgentStatus()
    return runtime


def test_runtime_reports_busy_operation_and_returns_online_after_completion(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    entered = Event()
    release = Event()

    def operation() -> str:
        entered.set()
        release.wait(timeout=5)
        return "ok"

    worker = Thread(
        target=lambda: runtime.run(
            operation, operation="aba_report", operation_id="operation-123"
        )
    )
    worker.start()
    assert entered.wait(timeout=2)

    busy = runtime.status()
    assert busy.state == "busy"
    assert busy.operation == "aba_report"
    assert busy.operation_id == "operation-123"
    assert busy.started_at is not None

    release.set()
    worker.join(timeout=2)
    online = runtime.status()
    assert online.state == "online"
    assert online.operation == ""
    assert online.operation_id == ""
    runtime.shutdown()


def test_runtime_exposes_only_attention_error_code(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)

    def operation() -> None:
        raise DomainError(409, "SEARCH_ACCESS_CHECK", "cookie=secret needs verification")

    with pytest.raises(DomainError):
        runtime.run(operation, operation="amazon_search", operation_id="operation-456")

    status = runtime.status()
    assert status.state == "attention_required"
    assert status.last_error_code == "SEARCH_ACCESS_CHECK"
    assert "secret" not in status.model_dump_json()
    runtime.shutdown()


def test_runtime_replays_completed_operation_after_restart(tmp_path: Path) -> None:
    calls = 0

    def operation() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"result": {"filename": "keywords.xlsx", "count": 7}}

    first = _runtime(tmp_path)
    expected = first.run(
        operation,
        operation="sellersprite_export",
        operation_id="operation-durable-1",
        request_key="rack:last-30-days",
    )
    first.shutdown()

    second = _runtime(tmp_path)
    replayed = second.run(
        operation,
        operation="sellersprite_export",
        operation_id="operation-durable-1",
        request_key="rack:last-30-days",
    )

    assert replayed == expected
    assert calls == 1
    second.shutdown()


def test_runtime_persists_json_compatible_product_values(tmp_path: Path) -> None:
    """A successful browser result must not disconnect while writing its replay cache."""

    calls = 0
    captured_at = datetime(2026, 11, 2, 13, 40, tzinfo=UTC)

    def operation() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"result": {"price": Decimal("12.34"), "captured_at": captured_at}}

    first = _runtime(tmp_path)
    returned = first.run(
        operation,
        operation="amazon_product",
        operation_id="operation-product-json",
        request_key="asin:B0DNMNZC6Y",
    )
    assert returned["result"] == {"price": Decimal("12.34"), "captured_at": captured_at}
    assert first.status().state == "online"
    first.shutdown()

    second = _runtime(tmp_path)
    replayed = second.run(
        operation,
        operation="amazon_product",
        operation_id="operation-product-json",
        request_key="asin:B0DNMNZC6Y",
    )
    assert replayed == {
        "result": {"price": 12.34, "captured_at": "2026-11-02T13:40:00+00:00"}
    }
    assert calls == 1
    assert second.status().state == "online"
    second.shutdown()


class StatusRuntime:
    def status(self) -> BrowserAgentStatus:
        return BrowserAgentStatus(
            state="busy",
            operation="sellersprite_export",
            operation_id="operation-789",
        )


def test_status_endpoint_requires_token_and_returns_safe_status() -> None:
    with TestClient(create_app(StatusRuntime(), token=TOKEN)) as client:  # type: ignore[arg-type]
        blocked = client.get("/v1/status")
        allowed = client.get(
            "/v1/status", headers={"Authorization": f"Bearer {TOKEN}"}
        )

    assert blocked.status_code == 401
    assert allowed.status_code == 200
    assert allowed.json() == {
        "state": "busy",
        "operation": "sellersprite_export",
        "operation_id": "operation-789",
        "started_at": None,
        "last_error_code": "",
        "version": "1",
    }
