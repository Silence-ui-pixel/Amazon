"""Windows 浏览器 Agent 的轻量网络边界。"""

from __future__ import annotations

import ipaddress
import os
import secrets

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

MIN_TOKEN_LENGTH = 32


def is_loopback_host(host: str) -> bool:
    normalized = host.strip().lower().strip("[]")
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def configured_token(value: str | None = None) -> str:
    return (value if value is not None else os.getenv("KEYWORD_BROWSER_AGENT_TOKEN", "")).strip()


def validate_agent_security(token: str, host: str) -> None:
    """拒绝弱 Token，以及未鉴权的非 loopback 监听。"""
    if token and len(token) < MIN_TOKEN_LENGTH:
        raise ValueError(f"浏览器 Agent Token 至少 {MIN_TOKEN_LENGTH} 个字符")
    if not token and not is_loopback_host(host):
        raise ValueError("未配置 Token 时，浏览器 Agent 不能监听非 loopback 地址")


def bearer_matches(request: Request, token: str) -> bool:
    if not token:
        return True
    scheme, _, presented = request.headers.get("authorization", "").partition(" ")
    return (
        scheme.lower() == "bearer"
        and bool(presented)
        and secrets.compare_digest(presented.encode("utf-8"), token.encode("utf-8"))
    )


class AgentAuthMiddleware:
    """只豁免存活探针，其余 Agent 路由统一要求 Bearer Token。"""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") == "/v1/health":
            await self.app(scope, receive, send)
            return

        if not self.token:
            server = scope.get("server")
            server_host = server[0] if server else ""
            if is_loopback_host(server_host):
                await self.app(scope, receive, send)
                return
            response = JSONResponse({"detail": "未鉴权模式仅允许 loopback 请求"}, status_code=403)
            await response(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        if bearer_matches(request, self.token):
            await self.app(scope, receive, send)
            return

        response = JSONResponse(
            {"detail": "浏览器 Agent 鉴权失败"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
        await response(scope, receive, send)
