"""浏览器 Agent 的 ASGI 应用；绑定 127.0.0.1，由服务器经隧道/VPN 访问。"""

import asyncio
import hashlib
import json
import os
import secrets
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from agent import ops
from agent.runtime import AgentRuntime
from agent.task_worker import AgentTaskWorker
from app.domain.errors import DomainError
from app.domain.sellersprite import BrowserCollectionInput


def paired_device_id() -> str:
    """上报本机配对到的设备号，页面即使清了 localStorage 也能找回自己的设备。"""
    base = os.environ.get("APPDATA") or ""
    if not base:
        return ""
    try:
        payload = json.loads(
            (Path(base) / "KeywordLibraryAgent" / "config.json").read_text(encoding="utf-8")
        )
        return str(payload.get("device_id") or "")
    except (OSError, ValueError, TypeError, AttributeError):
        return ""


class SessionRequest(BaseModel):
    marketplace: str
    lanes: int = Field(default=1, ge=1, le=4)


class SearchRequest(BaseModel):
    keyword: str
    page_number: int = Field(ge=1)
    evidence_name: str


class SearchesRequest(BaseModel):
    requests: list[SearchRequest] = Field(min_length=1, max_length=8)


class ProductRequest(BaseModel):
    asin: str


class ProductsRequest(BaseModel):
    asins: list[str] = Field(min_length=1, max_length=30)


class ProductOneShotRequest(BaseModel):
    marketplace: str
    asin: str


class AbaReportRequest(BaseModel):
    query: str
    week_end: str
    country: str = "us"
    max_rows: int = Field(default=1000, ge=1, le=10000)
    query_mode: Literal["root", "asin", "product_term"] = "root"
    rank_limit: int | None = Field(default=None, ge=1)


class PoeDownloadRequest(BaseModel):
    run_id: str
    keyword: str
    intent_ref: str = ""


class PoeIntentRequest(BaseModel):
    keyword: str = Field(min_length=1, max_length=100)


class StoreSelectionRequest(BaseModel):
    store_id: str = Field(min_length=1, max_length=40)


class UiActionRequest(BaseModel):
    action: Literal["hide", "open_data", "quit", "check_update"]


def create_app(runtime: AgentRuntime | None = None, *, server_only: bool = False) -> FastAPI:
    rt = runtime or AgentRuntime()
    worker = None if server_only else AgentTaskWorker.from_environment(rt)
    stop_worker = asyncio.Event()
    worker_task: asyncio.Task[None] | None = None

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
        nonlocal worker_task
        if worker is not None:
            worker_task = asyncio.create_task(worker.run(stop_worker))
        try:
            yield
        finally:
            stop_worker.set()
            if worker_task is not None:
                worker_task.cancel()
                with suppress(asyncio.CancelledError):
                    await worker_task
            rt.shutdown()

    app = FastAPI(title="关键词库浏览器 Agent", version="1", lifespan=lifespan)
    app.state.runtime = rt
    app.state.task_worker = worker
    # 网站在使用者浏览器里探测本机采集端。只放开状态探测，采集调用仍走服务器。
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"https?://(127\.0\.0\.1|localhost)(:\d+)?|https://[a-z0-9.-]+\.sslip\.io",
        allow_methods=["GET", "OPTIONS"],
        allow_headers=["*"],
        # Chrome 的 Private Network Access：公网 HTTPS 页面探测 127.0.0.1 会先
        # 发带 Access-Control-Request-Private-Network 的预检；不开这个开关，
        # Starlette 会直接回 400，浏览器就不会发真正的状态请求。
        allow_private_network=True,
    )

    @app.get("/v1/health")
    def health() -> dict[str, object]:
        return {"ok": True}

    @app.post("/v1/ui/show")
    def show_status_window() -> dict[str, bool]:
        from agent.status_ui import STATUS

        STATUS.show_requested.set()
        return {"ok": True}

    @app.get("/v1/ui", response_class=HTMLResponse)
    def status_window_html() -> HTMLResponse:
        from agent.status_ui import STATUS

        path = Path(__file__).with_name("status_window.html")
        content = path.read_text(encoding="utf-8")
        STATUS.window_revision = hashlib.sha256(content.encode()).hexdigest()[:16]
        return HTMLResponse(content.replace("__UI_REVISION__", STATUS.window_revision), headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY"})

    def require_window(request: Request) -> None:
        from agent.status_ui import STATUS

        if not secrets.compare_digest(request.headers.get("X-Collector-UI", ""), STATUS.ui_token):
            raise HTTPException(403, "请重新打开本机采集器窗口")

    @app.get("/v1/ui/state")
    def status_window_state(request: Request) -> dict[str, object]:
        from agent.status_ui import STATUS

        require_window(request)
        if not STATUS.window_revision:
            STATUS.window_revision = hashlib.sha256(Path(__file__).with_name("status_window.html").read_bytes()).hexdigest()[:16]
        return STATUS.snapshot()

    @app.post("/v1/ui/action")
    def status_window_action(request: Request, data: UiActionRequest) -> dict[str, bool]:
        from agent.status_ui import STATUS

        require_window(request)
        if data.action == "hide":
            STATUS.hide_requested.set()
        elif data.action == "quit":
            STATUS.shutdown_requested.set()
        elif data.action == "open_data":
            directory = Path(os.environ.get("APPDATA") or str(Path.home())) / "KeywordLibraryAgent" / "data"
            directory.mkdir(parents=True, exist_ok=True)
            os.startfile(str(directory))
        elif data.action == "check_update" and STATUS.snapshot()["update_phase"] not in {"checking", "downloading", "restarting"}:
            STATUS.set_update("已收到检查更新请求，正在等待采集器空闲", "queued")
            STATUS.reopen_after_update = True
            STATUS.update_requested.set()
        return {"ok": True}

    @app.get("/v1/ziniao/stores")
    def ziniao_stores() -> dict[str, object]:
        """列出本机紫鸟里的店铺；多店铺时页面据此让用户选择。"""
        from app.adapters.ziniao import list_stores

        try:
            stores = list_stores()
        except DomainError as error:
            return {"stores": [], "error": error.code, "message": error.message}
        current = ""
        try:
            current = (rt.data / "amazon" / "store_id").read_text(encoding="utf-8").strip()
        except OSError:
            current = ""
        return {"stores": stores, "current": current}

    @app.post("/v1/ziniao/store")
    def ziniao_store(data: StoreSelectionRequest) -> dict[str, object]:
        """选定紫鸟店铺；记住后采集会连这个店铺的浏览器。"""
        store_id = rt.select_store(data.store_id)
        return {"ok": True, "store_id": store_id}

    @app.get("/v1/status")
    def status() -> dict[str, object]:
        payload = dict(rt.status())
        device_id = paired_device_id()
        if device_id:
            payload["device_id"] = device_id
        return payload

    @app.post("/v1/session")
    def open_session(req: SessionRequest) -> dict[str, object]:
        return ops.open_session(rt, req.marketplace, req.lanes)

    @app.post("/v1/session/{session_id}/search")
    def search(session_id: str, req: SearchRequest) -> dict[str, object]:
        return ops.search(rt, session_id, req.keyword, req.page_number, req.evidence_name)

    @app.post("/v1/session/{session_id}/searches")
    def searches(session_id: str, req: SearchesRequest, request: Request) -> dict[str, object]:
        return ops.searches(rt, session_id, [item.model_dump() for item in req.requests], progress_id=request.headers.get("X-Keyword-Request-ID", ""))

    @app.post("/v1/session/{session_id}/product")
    def product(session_id: str, req: ProductRequest) -> dict[str, object]:
        return ops.product(rt, session_id, req.asin)

    @app.post("/v1/session/{session_id}/products")
    def products(session_id: str, req: ProductsRequest, request: Request) -> dict[str, object]:
        return ops.products(rt, session_id, req.asins, progress_id=request.headers.get("X-Keyword-Request-ID", ""))

    @app.delete("/v1/session/{session_id}")
    def close_session(session_id: str) -> dict[str, object]:
        return ops.close_session(rt, session_id)

    @app.post("/v1/product")
    def product_one_shot(req: ProductOneShotRequest) -> dict[str, object]:
        return ops.product_one_shot(rt, req.marketplace, req.asin)

    @app.post("/v1/aba/report")
    def aba_report(req: AbaReportRequest) -> dict[str, object]:
        return ops.aba_report(
            rt,
            req.query,
            req.week_end,
            country=req.country,
            max_rows=req.max_rows,
            query_mode=req.query_mode,
            rank_limit=req.rank_limit,
        )

    @app.post('/v1/aba/download-original')
    def aba_original(req: AbaReportRequest):
        return ops.platform_original(rt, req.model_dump(mode='json'))

    @app.post('/v1/sellersprite/download-original')
    def sellersprite_original(req: BrowserCollectionInput):
        return ops.platform_original(rt, req.model_dump(mode='json'), sellersprite=True)

    @app.get("/v1/aba/status")
    def aba_status() -> dict[str, object]:
        return ops.aba_status(rt)

    @app.post("/v1/sellersprite/export")
    def sellersprite_export(req: BrowserCollectionInput) -> dict[str, object]:
        return ops.sellersprite_export(rt, req)

    @app.post("/v1/poe/download")
    def poe_download(req: PoeDownloadRequest) -> dict[str, object]:
        return ops.poe_download(rt, req.run_id, req.keyword, req.intent_ref)

    @app.post("/v1/poe/intents")
    def poe_intents(req: PoeIntentRequest) -> dict[str, object]:
        return ops.poe_intents(rt, req.keyword)

    return app


app = create_app()
