"""访问者采集端的反向长连接通道。

客户端（用户电脑上的采集端）主动连服务器，服务器把浏览器操作帧发下去、等回包。
好处：不需要公网暴露紫鸟 CDP，也不需要给每个用户配 SSH 反向隧道。

帧格式与 HTTP 接口保持一致，都是 `{"result": ...}` / `{"error": ...}` 信封，
所以 AgentClient 只是换了传输层，业务侧完全无感。
"""

import asyncio
import threading
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from app.domain.errors import DomainError

CHANNEL_OFFLINE = "AGENT_CHANNEL_OFFLINE"
CHANNEL_TIMEOUT = "AGENT_CHANNEL_TIMEOUT"


@dataclass
class ChannelConnection:
    device_id: str
    queue: "asyncio.Queue[dict[str, object]]"
    loop: asyncio.AbstractEventLoop
    pending: dict[str, "asyncio.Future[dict[str, Any]]"] = field(default_factory=dict)
    closed: bool = False


class AgentChannelRegistry:
    """device_id -> 在线长连接；同步侧用 call()，异步侧用 resolve()/disconnect()。"""

    def __init__(self) -> None:
        self._connections: dict[str, ChannelConnection] = {}
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    # ---- 生命周期 -----------------------------------------------------
    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """由应用启动阶段调用，记录服务器自己的事件循环。"""
        self._loop = loop

    def connect(self, device_id: str) -> ChannelConnection:
        loop = self._loop
        if loop is None:
            raise DomainError(503, CHANNEL_OFFLINE, "服务器尚未就绪，请稍后重试")
        connection = ChannelConnection(
            device_id=device_id, queue=asyncio.Queue(), loop=loop
        )
        with self._lock:
            previous = self._connections.get(device_id)
            self._connections[device_id] = connection
        if previous is not None:
            # 同一设备重连：老连接上的等待者立即失败，避免挂死。
            previous.closed = True
            for future in list(previous.pending.values()):
                if not future.done():
                    future.set_exception(
                        DomainError(503, CHANNEL_OFFLINE, "采集端重新连接，本次操作已取消")
                    )
            previous.pending.clear()
        return connection

    def disconnect(self, device_id: str, connection: ChannelConnection) -> None:
        with self._lock:
            if self._connections.get(device_id) is connection:
                self._connections.pop(device_id, None)
        connection.closed = True
        for future in list(connection.pending.values()):
            if not future.done():
                future.set_exception(
                    DomainError(503, CHANNEL_OFFLINE, "采集端已断开，等待其重新连接")
                )
        connection.pending.clear()

    def is_connected(self, device_id: str) -> bool:
        with self._lock:
            connection = self._connections.get(device_id)
        return connection is not None and not connection.closed

    def connected_devices(self) -> list[str]:
        with self._lock:
            return [key for key, value in self._connections.items() if not value.closed]

    # ---- 收包 ---------------------------------------------------------
    def resolve(self, device_id: str, message: dict[str, object]) -> None:
        """把客户端的 response 帧交给对应的等待者。"""
        request_id = str(message.get("id", ""))
        with self._lock:
            connection = self._connections.get(device_id)
        if connection is None:
            return
        future = connection.pending.pop(request_id, None)
        if future is None or future.done():
            return
        body = message.get("body")
        future.set_result(body if isinstance(body, dict) else {})

    # ---- 发包（同步调用方）--------------------------------------------
    def call(
        self,
        device_id: str,
        method: str,
        path: str,
        body: dict[str, object] | None,
        *,
        operation_id: str = "",
        timeout: float = 600.0,
    ) -> dict[str, Any]:
        loop = self._loop
        if loop is None or not loop.is_running():
            raise DomainError(503, CHANNEL_OFFLINE, "服务器尚未就绪，请稍后重试")
        with self._lock:
            connection = self._connections.get(device_id)
        if connection is None or connection.closed:
            raise DomainError(
                503, CHANNEL_OFFLINE, "该设备上的采集端当前离线，请先启动客户端"
            )
        request_id = str(uuid4())
        frame: dict[str, object] = {
            "type": "request",
            "id": request_id,
            "method": method,
            "path": path,
            "body": body or {},
            "operation_id": operation_id,
        }
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._send_and_wait(connection, request_id, frame, timeout), loop
            )
            return future.result(timeout + 10)
        except TimeoutError:
            raise DomainError(504, CHANNEL_TIMEOUT, "采集端响应超时，请检查其是否卡住") from None

    async def _send_and_wait(
        self,
        connection: ChannelConnection,
        request_id: str,
        frame: dict[str, object],
        timeout: float,
    ) -> dict[str, Any]:
        pending: asyncio.Future[dict[str, Any]] = connection.loop.create_future()
        connection.pending[request_id] = pending
        try:
            await connection.queue.put(frame)
            return await asyncio.wait_for(pending, timeout)
        except TimeoutError:
            raise DomainError(504, CHANNEL_TIMEOUT, "采集端响应超时，请检查其是否卡住") from None
        finally:
            connection.pending.pop(request_id, None)
