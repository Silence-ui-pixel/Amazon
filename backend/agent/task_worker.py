"""Agent 主动领取浏览器操作并回传结果。"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from collections.abc import Mapping
from contextlib import suppress
from typing import cast

import httpx

from agent import ops
from agent.runtime import AgentRuntime
from app.domain.sellersprite import BrowserCollectionInput

LOGGER = logging.getLogger(__name__)
POLL_SECONDS = 3.0
HEARTBEAT_SECONDS = 20.0
LEASE_SECONDS = 120


class AgentTaskWorker:
    """使用设备凭据轮询服务器；浏览器动作仍由 ops 白名单执行。"""

    def __init__(
        self,
        runtime: AgentRuntime,
        *,
        control_url: str,
        device_id: str,
        device_token: str,
        agent_id: str,
        profile_id: str,
        profile_name: str,
        username: str = "",
        password: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.runtime = runtime
        self.control_url = control_url.rstrip("/")
        self.device_id = device_id
        self.device_token = device_token
        self.agent_id = agent_id
        self.profile_id = profile_id
        self.profile_name = profile_name
        self.auth = (username, password) if username and password else None
        self.transport = transport
        self.lease_owner = f"{socket.gethostname()}:{os.getpid()}:{agent_id}"
        self._sessions: dict[str, str] = {}

    @classmethod
    def from_environment(cls, runtime: AgentRuntime) -> AgentTaskWorker | None:
        values = {
            "control_url": os.getenv("KEYWORD_CONTROL_PLANE_URL", "").strip(),
            "device_id": os.getenv("KEYWORD_AGENT_DEVICE_ID", "").strip(),
            "device_token": os.getenv("KEYWORD_AGENT_DEVICE_TOKEN", "").strip(),
            "agent_id": os.getenv("KEYWORD_AGENT_ID", "").strip(),
            "profile_id": os.getenv("KEYWORD_AGENT_PROFILE_ID", "").strip(),
            "profile_name": os.getenv("KEYWORD_AGENT_PROFILE_NAME", "").strip(),
        }
        if not all(values.values()):
            return None
        return cls(
            runtime,
            control_url=values["control_url"],
            device_id=values["device_id"],
            device_token=values["device_token"],
            agent_id=values["agent_id"],
            profile_id=values["profile_id"],
            profile_name=values["profile_name"],
            username=os.getenv("KEYWORD_CONTROL_PLANE_USERNAME", "").strip(),
            password=os.getenv("KEYWORD_CONTROL_PLANE_PASSWORD", "").strip(),
        )

    async def run(self, stop: asyncio.Event) -> None:
        timeout = httpx.Timeout(30.0, connect=10.0)
        async with httpx.AsyncClient(
            base_url=self.control_url,
            timeout=timeout,
            trust_env=False,
            auth=self.auth,
            transport=self.transport,
        ) as client:
            while not stop.is_set():
                try:
                    await self._heartbeat(client)
                    response = await client.post(
                        f"/api/v1/browser/devices/{self.device_id}/claim",
                        json={
                            "token": self.device_token,
                            "lease_owner": self.lease_owner,
                            "lease_seconds": LEASE_SECONDS,
                        },
                    )
                    response.raise_for_status()
                    body = response.json()
                    claimed = body.get("operation")
                    if isinstance(claimed, dict):
                        operation = cast(dict[str, object], claimed)
                        await self._process(
                            client,
                            operation,
                            str(body.get("lease_token", "")),
                            stop,
                        )
                    else:
                        await self._pause(stop, POLL_SECONDS)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    LOGGER.exception("browser task polling failed")
                    await self._pause(stop, POLL_SECONDS)

    async def _heartbeat(self, client: httpx.AsyncClient) -> None:
        response = await client.post(
            f"/api/v1/browser/devices/{self.device_id}/heartbeat",
            json={
                "token": self.device_token,
                "profile_id": self.profile_id,
                "profile_name": self.profile_name,
                "capabilities": {
                    "amazon": True,
                    "aba": True,
                    "sellersprite": True,
                    "poe": True,
                },
                "status": "busy" if self.runtime.status().get("state") == "busy" else "online",
            },
        )
        response.raise_for_status()

    async def _process(
        self,
        client: httpx.AsyncClient,
        operation: dict[str, object],
        lease_token: str,
        stop: asyncio.Event,
    ) -> None:
        operation_id = str(operation.get("operation_id", ""))
        workflow_id = str(operation.get("workflow_id", ""))
        attempt = self._as_int(operation.get("attempt"), 0)
        payload = operation.get("payload")
        task_payload = cast(dict[str, object], payload) if isinstance(payload, dict) else {}
        if str(operation.get("profile_id", "")) != self.profile_id:
            await self._submit(
                client,
                operation_id,
                attempt,
                lease_token,
                status="failed",
                result={},
                files={},
                error="PROFILE_MISMATCH: task profile does not match the local Agent profile",
            )
            return

        done = asyncio.Event()
        lease_lost = asyncio.Event()
        renewal = asyncio.create_task(
            self._maintain_lease(client, operation_id, lease_token, done, lease_lost, stop)
        )
        try:
            envelope = await asyncio.to_thread(
                self._dispatch,
                str(operation.get("operation_type", "")),
                workflow_id,
                task_payload,
            )
            if lease_lost.is_set():
                LOGGER.warning("lease lost; discarding stale operation result: %s", operation_id)
                return
            error = envelope.get("error")
            if isinstance(error, dict):
                error_data = cast(dict[str, object], error)
                message = str(error_data.get("message", error_data))[:5000]
                code = str(error_data.get("code", "AGENT_OPERATION_FAILED"))
                await self._submit(
                    client,
                    operation_id,
                    attempt,
                    lease_token,
                    status="failed",
                    result={},
                    files=self._as_dict(envelope.get("files")),
                    error=f"{code}: {message}",
                )
            else:
                await self._submit(
                    client,
                    operation_id,
                    attempt,
                    lease_token,
                    status="succeeded",
                    result=self._as_dict(envelope.get("result")),
                    files=self._as_dict(envelope.get("files")),
                    error="",
                )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            LOGGER.exception("browser operation failed: %s", operation_id)
            try:
                await self._submit(
                    client,
                    operation_id,
                    attempt,
                    lease_token,
                    status="failed",
                    result={},
                    files={},
                    error=f"{type(error).__name__}: {error}"[:5000],
                )
            except Exception:
                LOGGER.exception("failed to report browser operation error: %s", operation_id)
        finally:
            done.set()
            renewal.cancel()
            with suppress(asyncio.CancelledError):
                await renewal

    def _dispatch(
        self, operation_type: str, workflow_id: str, payload: dict[str, object]
    ) -> dict[str, object]:
        """只派发明确列出的 Agent 操作，不执行任意服务器提供的代码。"""
        session_id = self._sessions.get(workflow_id, "")
        if operation_type == "open_session":
            envelope = ops.open_session(
                self.runtime,
                str(payload.get("marketplace", "us")),
                self._as_int(payload.get("lanes"), 1),
            )
            result = envelope.get("result")
            if isinstance(result, dict):
                session_data = cast(dict[str, object], result)
                session_id_value = session_data.get("session_id")
                if session_id_value:
                    self._sessions[workflow_id] = str(session_id_value)
            return envelope
        if operation_type == "search":
            return ops.search(
                self.runtime,
                self._required_session(session_id),
                str(payload["keyword"]),
                self._as_int(payload.get("page_number"), 1),
                str(payload.get("evidence_name", "")),
            )
        if operation_type == "searches":
            requests = payload.get("requests", [])
            if not isinstance(requests, list):
                raise ValueError("searches operation requires a requests list")
            request_rows = cast(list[object], requests)
            request_items = [
                cast(dict[str, object], item)
                for item in request_rows
                if isinstance(item, dict)
            ]
            return ops.searches(
                self.runtime,
                self._required_session(session_id),
                request_items,
            )
        if operation_type == "product":
            return ops.product(
                self.runtime, self._required_session(session_id), str(payload["asin"])
            )
        if operation_type == "products":
            asins = payload.get("asins", [])
            if not isinstance(asins, list):
                raise ValueError("products operation requires an asins list")
            return ops.products(
                self.runtime,
                self._required_session(session_id),
                [str(asin) for asin in cast(list[object], asins)],
            )
        if operation_type == "close_session":
            envelope = ops.close_session(self.runtime, self._required_session(session_id))
            self._sessions.pop(workflow_id, None)
            return envelope
        if operation_type == "product_one_shot":
            return ops.product_one_shot(
                self.runtime, str(payload.get("marketplace", "us")), str(payload["asin"])
            )
        if operation_type == "aba_report":
            return ops.aba_report(
                self.runtime,
                str(payload["query"]),
                str(payload["week_end"]),
                country=str(payload.get("country", "us")),
                max_rows=self._as_int(payload.get("max_rows"), 1000),
                rank_limit=self._as_int(payload.get("rank_limit"), 0) or None,
                query_mode=cast(str, payload.get("query_mode", "root")),  # type: ignore[arg-type]
            )
        if operation_type in ('aba_original', 'sellersprite_original'):
            return ops.platform_original(self.runtime, payload, sellersprite=operation_type == 'sellersprite_original')
        if operation_type == "sellersprite_export":
            request = BrowserCollectionInput.model_validate(payload)
            return ops.sellersprite_export(self.runtime, request)
        if operation_type == "poe_download":
            return ops.poe_download(
                self.runtime, str(payload["run_id"]), str(payload["keyword"]), str(payload.get("intent_ref", ""))
            )
        if operation_type == "poe_intents":
            return ops.poe_intents(self.runtime, str(payload["keyword"]))
        raise ValueError(f"Unsupported browser operation: {operation_type}")

    @staticmethod
    def _as_int(value: object, default: int) -> int:
        if isinstance(value, bool):
            return default
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            try:
                return int(value)
            except ValueError:
                return default
        return default

    @staticmethod
    def _required_session(session_id: str) -> str:
        if not session_id:
            raise ValueError("workflow browser session has not been opened")
        return session_id

    async def _maintain_lease(
        self,
        client: httpx.AsyncClient,
        operation_id: str,
        lease_token: str,
        done: asyncio.Event,
        lease_lost: asyncio.Event,
        stop: asyncio.Event,
    ) -> None:
        while not done.is_set() and not stop.is_set():
            try:
                await asyncio.wait_for(done.wait(), timeout=HEARTBEAT_SECONDS)
                return
            except TimeoutError:
                pass
            try:
                await self._heartbeat(client)
                response = await client.post(
                    f"/api/v1/browser/operations/{operation_id}/lease",
                    json={
                        "device_id": self.device_id,
                        "token": self.device_token,
                        "lease_owner": self.lease_owner,
                        "lease_token": lease_token,
                        "lease_seconds": LEASE_SECONDS,
                    },
                )
                response.raise_for_status()
            except Exception:
                LOGGER.exception("operation lease renewal failed: %s", operation_id)
                lease_lost.set()
                return

    async def _submit(
        self,
        client: httpx.AsyncClient,
        operation_id: str,
        attempt: int,
        lease_token: str,
        *,
        status: str,
        result: dict[str, object],
        files: dict[str, object],
        error: str,
    ) -> None:
        response = await client.post(
            f"/api/v1/browser/operations/{operation_id}/result",
            json={
                "device_id": self.device_id,
                "token": self.device_token,
                "lease_owner": self.lease_owner,
                "lease_token": lease_token,
                "attempt": attempt,
                "status": status,
                "result": result,
                "files": files,
                "error": error,
            },
        )
        response.raise_for_status()

    @staticmethod
    def _as_dict(value: object) -> dict[str, object]:
        return cast(dict[str, object], value) if isinstance(value, Mapping) else {}

    @staticmethod
    async def _pause(stop: asyncio.Event, seconds: float) -> None:
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=seconds)
