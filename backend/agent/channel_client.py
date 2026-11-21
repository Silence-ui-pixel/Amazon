"""采集端连接桥：把服务器的反向长连接桥接到本机 127.0.0.1:7100 的 Agent。

这样做的好处是不用改 Agent 内部逻辑：服务器发下来的每个请求帧，仍然用同一套
HTTP 端点和同一份 JSON 信封处理，只是外层传输换成了客户端主动发起的长连接。

用法（环境变量）：
    KEYWORD_CONTROL_PLANE_WS   wss://<服务器域名>/api/v1/agent/connect
    KEYWORD_AGENT_DEVICE_ID    设备登记返回的 device_id
    KEYWORD_AGENT_DEVICE_TOKEN 设备登记时提交的 token
可选：
    KEYWORD_AGENT_LOCAL_URL    默认 http://127.0.0.1:7100
    KEYWORD_AGENT_VERSION      展示用版本号
"""

import asyncio
import json
import os
import sys
from typing import Any

import httpx
import websockets

CONNECT_TIMEOUT = 20.0
RECONNECT_SECONDS = 5.0
# ABA/卖家精灵可能跑几分钟，帧本身很小，但留足余量避免被截断。
MAX_FRAME = 32 * 1024 * 1024


async def _handle(
    websocket: Any, frame: dict[str, object], client: httpx.AsyncClient
) -> None:
    request_id = str(frame.get("id", ""))
    method = str(frame.get("method", "GET")).upper()
    path = str(frame.get("path", "/"))
    body = frame.get("body")
    operation_id = str(frame.get("operation_id", "") or "").strip()
    headers = {"X-Keyword-Operation-ID": operation_id} if operation_id else {}
    try:
        response = await client.request(
            method,
            path,
            json=body if isinstance(body, dict) else None,
            headers=headers or None,
        )
        try:
            payload: Any = response.json()
        except ValueError:
            payload = {
                "error": {
                    "status_code": 502,
                    "code": "AGENT_INVALID_RESPONSE",
                    "message": "本机 Agent 返回非 JSON",
                }
            }
    except httpx.HTTPError as error:
        payload = {
            "error": {
                "status_code": 503,
                "code": "AGENT_UNAVAILABLE",
                "message": f"本机 Agent 不可达（{type(error).__name__}）",
            }
        }
    if not isinstance(payload, dict):
        payload = {"error": {"status_code": 502, "code": "AGENT_INVALID_RESPONSE", "message": "结构异常"}}
    await websocket.send(
        json.dumps({"type": "response", "id": request_id, "body": payload}, ensure_ascii=False)
    )


async def _session(
    url: str, device_id: str, token: str, local: str, version: str, basic: str
) -> None:
    headers = {"Authorization": f"Basic {basic}"} if basic else None
    async with websockets.connect(
        url, max_size=MAX_FRAME, open_timeout=CONNECT_TIMEOUT, additional_headers=headers
    ) as websocket:
        await websocket.send(
            json.dumps(
                {"type": "hello", "device_id": device_id, "token": token, "version": version}
            )
        )
        print(f"[channel] connected as {device_id}", flush=True)
        tasks: set[asyncio.Task[None]] = set()
        async with httpx.AsyncClient(base_url=local, timeout=httpx.Timeout(900.0)) as client:
            async for raw in websocket:
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(message, dict) or message.get("type") != "request":
                    continue
                task = asyncio.create_task(_handle(websocket, message, client))
                tasks.add(task)
                task.add_done_callback(tasks.discard)


async def run() -> None:
    url = os.environ.get("KEYWORD_CONTROL_PLANE_WS", "").strip()
    device_id = os.environ.get("KEYWORD_AGENT_DEVICE_ID", "").strip()
    token = os.environ.get("KEYWORD_AGENT_DEVICE_TOKEN", "").strip()
    local = os.environ.get("KEYWORD_AGENT_LOCAL_URL", "http://127.0.0.1:7100").rstrip("/")
    version = os.environ.get("KEYWORD_AGENT_VERSION", "").strip()
    # 站点在同域开了 Basic Auth 时必须一起带上，否则升级请求会被 401 挡掉。
    basic = os.environ.get("KEYWORD_CONTROL_PLANE_BASIC", "").strip()
    missing = [
        name
        for name, value in (
            ("KEYWORD_CONTROL_PLANE_WS", url),
            ("KEYWORD_AGENT_DEVICE_ID", device_id),
            ("KEYWORD_AGENT_DEVICE_TOKEN", token),
        )
        if not value
    ]
    if missing:
        print(f"[channel] 缺少配置：{', '.join(missing)}", file=sys.stderr)
        return
    while True:
        try:
            await _session(url, device_id, token, local, version, basic)
        except Exception as error:  # noqa: BLE001 - 断线后必须继续重连
            print(f"[channel] disconnected: {type(error).__name__}: {error}", flush=True)
        await asyncio.sleep(RECONNECT_SECONDS)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
