"""安全启动 Windows 浏览器 Agent；监听地址与安全校验使用同一配置。"""

from __future__ import annotations

import os
from pathlib import Path

import uvicorn

from agent.main import create_app
from agent.security import AgentAuthMiddleware, configured_token, validate_agent_security


def main() -> None:
    host = os.getenv("KEYWORD_AGENT_HOST", "127.0.0.1").strip()
    port = int(os.getenv("KEYWORD_AGENT_PORT", "7100"))
    token = configured_token()
    collector_socket = os.getenv("KEYWORD_SERVER_COLLECTOR_SOCKET", "").strip()
    validate_agent_security(token, host)
    if collector_socket and (not Path(collector_socket).is_absolute() or not token):
        raise ValueError("服务器采集器需要绝对 Socket 路径和 Agent Token")

    app = create_app(server_only=bool(collector_socket))
    app.add_middleware(AgentAuthMiddleware, token=token)
    if collector_socket:
        uvicorn.run(app, uds=collector_socket, workers=1)
    else:
        uvicorn.run(app, host=host, port=port, workers=1)


if __name__ == "__main__":
    main()
