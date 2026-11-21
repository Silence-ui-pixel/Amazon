"""采集端反向长连接端点：客户端主动连服务器，服务器把操作帧发下去。

握手：客户端连上后先发 {"type":"hello","device_id":...,"token":...}，
服务器用设备登记表里的 token 校验（常量时间比较），失败直接关闭连接。
之后服务器下发 {"type":"request",...}，客户端回 {"type":"response","id":...,"body":...}。
"""

import asyncio
import hmac
from contextlib import suppress
from typing import Any, cast

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.domain.errors import DomainError
from app.services.agent_channel import AgentChannelRegistry, ChannelConnection
from app.services.browser_devices import BrowserDeviceStore

HELLO_TIMEOUT = 15.0


async def _pump(websocket: WebSocket, connection: ChannelConnection) -> None:
    """把队列里的请求帧持续写给客户端。"""
    try:
        while True:
            frame = await connection.queue.get()
            await websocket.send_json(frame)
    except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
        return


def build_agent_channel_router(
    registry: AgentChannelRegistry, store: BrowserDeviceStore
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/agent", tags=["agent-channel"])

    @router.websocket("/connect")
    async def connect(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            hello: Any = await asyncio.wait_for(websocket.receive_json(), timeout=HELLO_TIMEOUT)
        except (TimeoutError, WebSocketDisconnect, ValueError):
            await websocket.close(code=4400)
            return
        if not isinstance(hello, dict):
            await websocket.close(code=4400)
            return
        hello = cast(dict[str, object], hello)
        device_id = str(hello.get("device_id", ""))
        token = str(hello.get("token", ""))
        version = str(hello.get("version", ""))
        try:
            device = store.get(device_id)
        except DomainError:
            await websocket.close(code=4404)
            return
        if not hmac.compare_digest(device.agent_token, token):
            await websocket.close(code=4401)
            return

        connection = registry.connect(device_id)
        store.mark(device_id, status="online", agent_version=version)
        sender = asyncio.create_task(_pump(websocket, connection))
        try:
            while True:
                message: Any = await websocket.receive_json()
                if not isinstance(message, dict):
                    continue
                message = cast(dict[str, object], message)
                kind = message.get("type")
                if kind == "response":
                    registry.resolve(device_id, message)
                elif kind == "ping":
                    await websocket.send_json({"type": "pong"})
        except (WebSocketDisconnect, RuntimeError, ValueError):
            pass
        finally:
            sender.cancel()
            registry.disconnect(device_id, connection)
            # 设备可能在连接期间被删除，此时不需要回写状态。
            # A replaced socket can close after the new connection is online.
            if not registry.is_connected(device_id):
                with suppress(DomainError):
                    store.mark(device_id, status="offline", agent_version=version)

    @router.get("/channel-probe")
    def channel_probe(device_id: str) -> dict[str, object]:
        """端到端诊断：从服务器经长连接问一次访问者本机的 7100 运行时。"""
        if not registry.is_connected(device_id):
            return {"ok": False, "error": "device_offline"}
        try:
            payload = registry.call(device_id, "GET", "/v1/status", None, timeout=20.0)
            return {"ok": True, "status": payload}
        except Exception as error:  # noqa: BLE001 - 诊断接口要如实回报
            return {"ok": False, "error": type(error).__name__ + ": " + str(error)[:200]}

    return router
