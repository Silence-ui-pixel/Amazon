"""一体化采集端入口：一个 exe 同时提供本机 7100 运行时和服务器反向长连接。

以前的客户端只是 WS→7100 桥接，别人电脑没有 Python / 项目源码，7100 就不存在：
页面探测失败显示「未连接」，服务器转发任务也会 AGENT_UNAVAILABLE。
这里把 AgentRuntime 直接内嵌进同一个进程，启动后先起 127.0.0.1:7100，
再跑客户端主循环，装一个 exe 就能完整采集。
"""

import os
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

# 与 client_app 一致的冻结环境 SSL 补丁，必须在任何 HTTPS 库被使用前生效。
if getattr(sys, "frozen", False):
    import _ssl
    import ssl

    def _frozen_ssl_context_new(cls, protocol=None, *args, **kwargs):
        if protocol is None:
            protocol = ssl.PROTOCOL_TLS
        return _ssl._SSLContext.__new__(cls, protocol)

    ssl.SSLContext.__new__ = staticmethod(_frozen_ssl_context_new)


def app_data_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "KeywordLibraryAgent" / "data"


def prepare_environment() -> int:
    """把会话/凭据目录固定到用户目录，避免 onefile 临时目录重启后丢失。"""
    data = app_data_dir()
    data.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("KEYWORD_AGENT_DATA_DIR", str(data))
    os.environ.setdefault("KEYWORD_STORAGE_ROOT", str(data))
    return int(os.environ.get("KEYWORD_AGENT_PORT", "7100"))


def start_runtime(port: int) -> None:
    """在后台线程里跑 uvicorn；显式指定实现模块，避免 PyInstaller 动态 import 丢失。"""
    import uvicorn

    from agent.main import app

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        log_config=None,
        access_log=False,
        loop="asyncio",
        http="h11",
        lifespan="on",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True, name="keyword-agent-runtime")
    thread.start()

    import httpx

    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        try:
            httpx.get(f"http://127.0.0.1:{port}/v1/health", timeout=1.0, trust_env=False)
            return
        except httpx.HTTPError:
            time.sleep(0.25)


def write_startup_log(stage: str, error: BaseException | None = None) -> None:
    """Keep a local diagnostic trail for windowed builds that have no console."""
    try:
        log = app_data_dir().parent / "startup.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        text = f"{datetime.now().isoformat(timespec='seconds')} {stage}"
        if error is not None:
            text += "\\n" + "".join(traceback.format_exception(error))
        with log.open("a", encoding="utf-8") as stream:
            stream.write(text + "\\n")
    except OSError:
        pass


def main() -> int:
    stage = "prepare_environment"
    try:
        from agent import client_app

        if client_app.acquire_single_instance() is None:
            client_app.show_existing_window()
            return 0
        port = prepare_environment()
        stage = "start_runtime"
        start_runtime(port)
        stage = "client_main"
        return client_app.main(instance_acquired=True)
    except SystemExit:
        # 正常退出路径（含单实例互斥直接返回、安排更新后重启），不是故障。
        raise
    except BaseException as error:
        write_startup_log(stage, error)
        raise


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as error:
        write_startup_log("unhandled", error)
        sys.exit(1)
