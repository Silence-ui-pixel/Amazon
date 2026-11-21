"""Carry the visitor's selected collector through HTTP handlers and background submits."""
from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from app.adapters.remote_browser import browser_device_context

BROWSER_DEVICE_HEADER = "X-Keyword-Browser-Device"


class BrowserDeviceContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        device_id = Request(scope).headers.get(BROWSER_DEVICE_HEADER)
        with browser_device_context(device_id.strip() if device_id else None):
            await self.app(scope, receive, send)
