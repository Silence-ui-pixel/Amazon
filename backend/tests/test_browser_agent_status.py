from __future__ import annotations

from datetime import UTC, datetime

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.adapters.remote_browser import AgentClient
from app.api.v1.browser_agent import build_browser_agent_router
from app.services.browser_agent_status import BrowserAgentStatusService


def test_unconfigured_agent_is_offline_without_probe() -> None:
    status = BrowserAgentStatusService(None).get()

    assert status.state == "offline"
    assert status.last_error_code == "AGENT_NOT_CONFIGURED"


def test_status_cache_and_forced_refresh_keep_only_safe_fields() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "state": "busy" if calls == 1 else "online",
                "operation": "aba_report" if calls == 1 else "",
                "operation_id": "op-1" if calls == 1 else "",
                "started_at": "2026-11-02T12:00:00Z" if calls == 1 else None,
                "last_error_code": "",
                "version": "0.1.0",
                "cookie": "must-not-leak",
                "detail": "private stack",
            },
        )

    client = AgentClient(
        "http://agent",
        token="agent-token-0123456789abcdef-0123456789",
        transport=httpx.MockTransport(handler),
    )
    service = BrowserAgentStatusService(client, now=lambda: datetime(2026, 11, 2, 12, 1, tzinfo=UTC))

    first = service.get()
    cached = service.get()
    refreshed = service.get(refresh=True)

    assert calls == 2
    assert first.state == cached.state == "busy"
    assert first.operation == "aba_report"
    assert refreshed.state == "online"
    assert "cookie" not in first.model_dump()
    assert "detail" not in first.model_dump()


def test_connection_error_is_normalized_and_refresh_route_only_probes() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("secret internal endpoint")

    client = AgentClient(
        "http://agent",
        token="agent-token-0123456789abcdef-0123456789",
        transport=httpx.MockTransport(handler),
    )
    service = BrowserAgentStatusService(client)
    app = FastAPI()
    app.include_router(build_browser_agent_router(service))

    with TestClient(app) as api:
        first = api.get("/api/v1/browser-agent/status")
        refreshed = api.post("/api/v1/browser-agent/status/refresh")

    assert calls == 2
    assert first.json()["state"] == "offline"
    assert refreshed.json()["last_error_code"] == "AGENT_UNAVAILABLE"
    assert "secret internal endpoint" not in refreshed.text
