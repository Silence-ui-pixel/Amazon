"""采集端配对与下载：页面发码，客户端凭码换凭据，然后下载安装包。"""

import secrets
from uuid import uuid4
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse
from pydantic import Field

from app.domain.errors import DomainError
from app.domain.schemas import Contract
from app.services.agent_channel import AgentChannelRegistry
from app.services.agent_pairing import PairingCodeStore
from app.services.browser_devices import BrowserDeviceStore


class PairRequest(Contract):
    code: str = Field(min_length=8, max_length=16)
    display_name: str = Field(min_length=1, max_length=200)
    version: str = Field(default="", max_length=50)


class PairResponse(Contract):
    device_id: str
    token: str
    ws_url: str
    channel_connected: bool


class IssueResponse(Contract):
    code: str
    expires_at: datetime


def _public_ws_url(request: Request) -> str:
    """从请求头推导客户端该连的地址，避免在客户端里写死域名。"""
    forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    scheme = forwarded or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    # The site only serves WebSocket over 443; port 80 redirects to HTTPS.
    # request.url.scheme is http behind the reverse proxy, so never emit ws:// here.
    ws_scheme = "wss"
    return f"{ws_scheme}://{host}/api/v1/agent/connect"


def build_agent_pairing_router(
    codes: PairingCodeStore,
    store: BrowserDeviceStore,
    registry: AgentChannelRegistry,
    binary: Path,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/agent", tags=["agent-pairing"])

    @router.post("/pairing-codes", status_code=201)
    def issue_code() -> IssueResponse:
        code, expires_at = codes.issue()
        return IssueResponse(code=code, expires_at=expires_at)

    @router.post("/pair")
    def pair(data: PairRequest, request: Request) -> PairResponse:
        # 先消费码再登记；失败时码已作废，避免被重放。
        codes.consume(data.code)
        # 绑定后再返回，页面生成这个码后即可轮询到该设备
        token = secrets.token_urlsafe(32)
        device = store.register(
            display_name=data.display_name,
            # 每台电脑必须是独立设备：store 按 agent_url 去重，共用
            # channel://client 会让后来配对的人覆盖前一个人的凭据。
            agent_url=f"channel://{uuid4().hex}",
            agent_token=token,
        )
        codes.bind(data.code, device.device_id)
        return PairResponse(
            device_id=device.device_id,
            token=device.agent_token,
            ws_url=_public_ws_url(request),
            channel_connected=registry.is_connected(device.device_id),
        )

    @router.get("/download")
    def download() -> FileResponse:
        if not binary.is_file():
            raise DomainError(
                404, "AGENT_BINARY_MISSING", "采集端安装包尚未上传，请联系管理员"
            )
        return FileResponse(
            binary,
            media_type="application/vnd.microsoft.portable-executable",
            filename="KeywordLibraryAgent.exe",
            headers={
                "Content-Disposition": "attachment; filename*=UTF-8''"
                + quote("KeywordLibraryAgent.exe")
            },
        )

    @router.get("/pairing-status")
    def pairing_status(device_id: str = "", code: str = "") -> dict[str, object]:
        """页面可用配对码或已保存的 device_id 查询『我这台电脑』的连接状态。"""
        resolved = device_id or codes.lookup(code)
        display_name = ""
        stale = False
        if resolved:
            try:
                device = store.get(resolved)
                display_name = device.display_name
                # 旧版共用 channel://client 的设备必须重新配对，否则多用户串设备。
                stale = device.agent_url.rstrip("/") == "channel://client"
            except DomainError:
                resolved = ""
        connected = registry.is_connected(resolved) if resolved else False
        return {
            "paired": bool(resolved),
            "device_id": resolved,
            "channel_connected": connected,
            "display_name": display_name,
            "stale": stale,
        }

    return router


def installer_path(storage_root: Path) -> Path:
    return storage_root / "agent" / "KeywordLibraryAgent.exe"
