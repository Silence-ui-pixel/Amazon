"""Windows 紫鸟 Agent 的网络边界与 Bearer 鉴权。"""

import asyncio
from collections.abc import Callable
from typing import TypeVar

import pytest
from fastapi.testclient import TestClient

import agent.main as agent_main
from agent.main import create_app
from agent.security import AgentAuthMiddleware
from app.domain.competitors import PoeIntentCandidate

TOKEN = "agent-token-0123456789abcdef-0123456789"
T = TypeVar("T")


def test_main_module_does_not_export_cli_bypass_app() -> None:
    assert not hasattr(agent_main, "app")


def test_unauthenticated_business_request_is_blocked_on_non_loopback_socket() -> None:
    reached = False
    sent: list[dict[str, object]] = []

    async def inner(scope: object, receive: object, send: object) -> None:
        nonlocal reached
        reached = True

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    scope: dict[str, object] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/session",
        "raw_path": b"/v1/session",
        "query_string": b"",
        "headers": [],
        "client": ("198.51.100.10", 50000),
        "server": ("192.168.1.20", 7100),
    }
    middleware = AgentAuthMiddleware(inner, "")  # type: ignore[arg-type]
    asyncio.run(middleware(scope, receive, send))  # type: ignore[arg-type]

    assert reached is False
    assert sent[0]["status"] == 403


class Runtime:
    def open_session(self, marketplace: str, *, operation_id: str = "") -> str:
        return f"session-{marketplace}"


class PoeRuntime(Runtime):
    class Poe:
        def intents(self, keyword: str) -> list[PoeIntentCandidate]:
            assert keyword == "dumbbell rack"
            return [
                PoeIntentCandidate(
                    ref="/opportunity-explorer/explore/niche/niche-1",
                    name="adjustable dumbbell stand",
                    top_keywords=["powerblock stand"],
                )
            ]

    poe = Poe()

    def run(self, fn: Callable[[], T], **_kwargs: object) -> T:
        return fn()


def test_health_is_available_without_token() -> None:
    with TestClient(create_app(Runtime(), token=TOKEN)) as client:  # type: ignore[arg-type]
        response = client.get("/v1/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


@pytest.mark.parametrize("authorization", [None, "Bearer wrong-token"])
def test_business_endpoint_rejects_missing_or_wrong_token(authorization: str | None) -> None:
    headers = {"Authorization": authorization} if authorization else {}
    with TestClient(create_app(Runtime(), token=TOKEN)) as client:  # type: ignore[arg-type]
        response = client.post("/v1/session", json={"marketplace": "US"}, headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {"detail": "浏览器 Agent 鉴权失败"}


def test_business_endpoint_accepts_correct_token() -> None:
    with TestClient(create_app(Runtime(), token=TOKEN)) as client:  # type: ignore[arg-type]
        response = client.post(
            "/v1/session",
            json={"marketplace": "US"},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
    assert response.status_code == 200
    assert response.json() == {"result": {"session_id": "session-US"}}


def test_poe_intent_endpoint_requires_token_and_returns_structured_candidates() -> None:
    with TestClient(create_app(PoeRuntime(), token=TOKEN)) as client:  # type: ignore[arg-type]
        blocked = client.post("/v1/poe/intents", json={"keyword": "dumbbell rack"})
        accepted = client.post(
            "/v1/poe/intents",
            json={"keyword": "dumbbell rack"},
            headers={"Authorization": f"Bearer {TOKEN}", "X-Keyword-Operation-ID": "op-1"},
        )
    assert blocked.status_code == 401
    assert accepted.status_code == 200
    assert accepted.json()["result"][0]["name"] == "adjustable dumbbell stand"


def test_no_token_is_allowed_only_on_loopback() -> None:
    create_app(Runtime(), token="", host="127.0.0.1")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="不能监听非 loopback"):
        create_app(Runtime(), token="", host="0.0.0.0")  # type: ignore[arg-type]


def test_configured_token_must_be_at_least_32_characters() -> None:
    with pytest.raises(ValueError, match="至少 32"):
        create_app(Runtime(), token="short", host="127.0.0.1")  # type: ignore[arg-type]
