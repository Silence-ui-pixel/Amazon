"""浏览器采集端状态 API；只返回服务器归一化后的安全字段。"""

from typing import Literal

from fastapi import APIRouter

from app.services.browser_agent_status import BrowserAgentStatusService, BrowserAgentStatusView


def build_browser_agent_router(
    service: BrowserAgentStatusService, *, server_only: bool = False
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/browser-agent", tags=["browser-agent"])

    @router.get("/mode")
    def mode() -> dict[str, Literal["server", "device"]]:
        return {"mode": "server" if server_only else "device"}

    @router.get("/status")
    def status() -> BrowserAgentStatusView:
        return service.get()

    @router.post("/status/refresh")
    def refresh() -> BrowserAgentStatusView:
        return service.get(refresh=True)

    return router
