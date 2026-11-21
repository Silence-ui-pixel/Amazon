"""本地 API 入口：组装路由、数据库生命周期和统一错误响应。"""

import asyncio
import os
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path
from uuid import uuid4

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from starlette.responses import Response

from app.adapters.aba import AbaClient, AbaProvider
from app.adapters.poe_downloads import PoeDownloadStore
from app.adapters.remote_browser import (
    AgentClient,
    AgentEndpoint,
    RemoteAbaClient,
    RemotePoeDownloader,
    RemoteProductProvider,
    RemoteSearchProvider,
    RemoteSellerspriteDownloader,
    current_browser_device_id,
    validate_agent_settings,
)
from app.api.device_context import BrowserDeviceContextMiddleware
from app.api.v1.aba import build_aba_router
from app.api.v1.aba_expansion import build_expansion_router
from app.api.v1.agent_channel import build_agent_channel_router
from app.api.v1.agent_pairing import build_agent_pairing_router, installer_path
from app.api.v1.browser_agent import build_browser_agent_router
from app.api.v1.browser_devices import build_browser_device_router
from app.api.v1.cleaning import build_cleaning_router
from app.api.v1.competitors import build_competitor_router
from app.api.v1.core_terms import build_core_router
from app.api.v1.hot_search_collection import build_hot_search_collection_router
from app.api.v1.hot_search_core_analysis import build_hot_search_core_analysis_router
from app.api.v1.images import build_image_router
from app.api.v1.library import build_library_router
from app.api.v1.model_config import build_model_config_router
from app.api.v1.product_profiles import build_product_profile_router
from app.api.v1.product_term_analysis import build_product_term_analysis_router
from app.api.v1.research_records import build_research_router
from app.api.v1.result_loading import build_result_loading_router, install_result_cache
from app.api.v1.roots import build_root_router
from app.api.v1.routes import build_router
from app.api.v1.third_party import build_third_party_router
from app.api.v1.traffic import build_traffic_router
from app.api.v1.understanding import build_understanding_router
from app.api.v1.verifications import build_verification_router
from app.api.v1.workflows import build_workflow_router
from app.db import config
from app.db.engine import build_engine
from app.db.repository import Repository
from app.domain.errors import DomainError
from app.services.aba import AbaService
from app.services.aba_expansion import ExpansionService
from app.services.agent_channel import AgentChannelRegistry
from app.services.agent_pairing import PairingCodeStore
from app.services.amazon_search import AmazonSearch, SearchProvider
from app.services.asin_product import AsinProductService, ProductProvider
from app.services.browser_agent_status import BrowserAgentStatusService
from app.services.browser_devices import BrowserDeviceStore
from app.services.cleaning import CleaningService
from app.services.competitors import CompetitorService
from app.services.core_terms import CoreService
from app.services.hot_search_collection import HotSearchCollectionService
from app.services.hot_search_core_analysis import HotSearchCoreAnalysisService
from app.services.images import ImageService
from app.services.library import LibraryService
from app.services.library_relevance import LibraryRelevanceService
from app.services.product_confirmation import ProductConfirmationService
from app.services.product_materials import ProductMaterialsService
from app.services.product_profiles import ProductProfileService
from app.services.product_term_analysis import ProductTermAnalysisService
from app.services.product_understanding import ProductUnderstandingService
from app.services.qwen import QwenClient
from app.services.roots import RootService
from app.services.sellersprite_collection import SellerspriteCollectionService
from app.services.third_party import ThirdPartyService
from app.services.traffic import TrafficService
from app.services.traffic_auto_review import TrafficAutoReviewService
from app.services.verification import VerificationService
from app.workflows.agent_recovery import AgentRecoveryScheduler
from app.workflows.browser_operations import BrowserOperationStore
from app.workflows.checkpointer import Checkpointer, postgres_checkpointer
from app.workflows.deps import WorkflowDeps
from app.workflows.graph import build_graph
from app.workflows.runner import WorkflowRunner
from app.workflows.service import WorkflowService
from app.workflows.store import WorkflowStore


def error_response(
    request: Request, status: int, code: str, message: str, details: list[dict[str, str]]
) -> JSONResponse:
    """统一业务及参数错误结构，用请求 ID 关联客户端问题与服务端排查。"""
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": details,
                "request_id": str(request.state.request_id),
            }
        },
    )


def create_app(
    database_url: str | None = None,
    *,
    storage_root: Path | None = None,
    model_client: QwenClient | None = None,
    search_provider: SearchProvider | None = None,
    auto_execute: bool = True,
    aba_client: AbaProvider | None = None,
    product_provider: ProductProvider | None = None,
    workflow_checkpointer: Checkpointer | None = None,
    workflow_inline: bool = False,
    browser_agent_url: str | None = None,
) -> FastAPI:
    """显式注入优先，便于测试隔离；默认从环境变量读取连接串与文件根目录。"""
    url = database_url if database_url is not None else config.database_url()
    engine = build_engine(url)
    root = storage_root if storage_root is not None else config.storage_root()
    repository = Repository(engine, root)
    # 业务库、工作流投影与图检查点同库持久化；采集器/执行器使用进程内锁，部署时保持单进程。
    workflow_store = WorkflowStore(engine)
    stack = ExitStack()
    collector_socket = os.environ.get("KEYWORD_SERVER_COLLECTOR_SOCKET", "").strip()
    server_only = bool(collector_socket)
    configured_agent_url = browser_agent_url or os.environ.get("KEYWORD_BROWSER_AGENT_URL", "")
    if server_only:
        socket_path = Path(collector_socket)
        if not socket_path.is_absolute():
            raise ValueError("服务器采集 Socket 必须使用绝对路径")
        if configured_agent_url.strip():
            raise ValueError("服务器采集模式不能配置远程 Agent URL")
        configured_agent_url = "http://server-collector"
    configured_agent_token = os.environ.get("KEYWORD_BROWSER_AGENT_TOKEN", "")
    agent_url, agent_token = validate_agent_settings(configured_agent_url, configured_agent_token)
    # 每台访问者设备各有一个可达地址；工作流绑定哪台，就只往哪台发浏览器操作。
    device_store = BrowserDeviceStore(root / "browser_devices.json")
    # 采集端主动连上来的反向通道；连接在进程内存里，部署保持单 worker。
    agent_channel = AgentChannelRegistry()
    # 页面签发的一次性配对码，同样只在单进程内存里。
    pairing_codes = PairingCodeStore()
    device_routing_enabled = os.environ.get(
        "KEYWORD_BROWSER_DEVICE_ROUTING", ""
    ).strip().casefold() in {"1", "true", "yes", "on"}

    def resolve_device_endpoint() -> AgentEndpoint | None:
        device_id = current_browser_device_id()
        if not device_id:
            return None
        device = device_store.get(device_id)
        if device.agent_url.startswith("channel://"):
            # 纯长连接设备没有 HTTP 地址，只能走 agent_channel。
            return None
        return AgentEndpoint(device.agent_url, device.agent_token)

    agent_client: AgentClient | None = None
    agent_recovery: AgentRecoveryScheduler | None = None
    remote_poe: RemotePoeDownloader | None = None
    remote_sellersprite: RemoteSellerspriteDownloader | None = None
    if agent_url or device_routing_enabled:
        agent_client = AgentClient(
            agent_url,
            token=agent_token,
            resolver=None if server_only else resolve_device_endpoint,
            channel=None if server_only else agent_channel,
            transport=httpx.HTTPTransport(uds=collector_socket) if server_only else None,
        )
        search_provider = search_provider or RemoteSearchProvider(agent_client)
        product_provider = product_provider or RemoteProductProvider(agent_client)
        aba_client = aba_client or RemoteAbaClient(agent_client)
        remote_poe = RemotePoeDownloader(
            agent_client, PoeDownloadStore(repository.storage_root / "poe_downloads")
        )
        remote_sellersprite = RemoteSellerspriteDownloader(agent_client)
    browser_agent_status = BrowserAgentStatusService(agent_client)
    saver = (
        workflow_checkpointer
        if workflow_checkpointer is not None
        else stack.enter_context(postgres_checkpointer(config.psycopg_url(url)))
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
        # 反向长连接要把同步调用方投递到这个事件循环上，先登记。
        agent_channel.bind_loop(asyncio.get_running_loop())
        # 在应用启动阶段初始化数据库，避免导入模块时就创建文件。
        repository.initialize()
        workflow_store.initialize()
        verification.recover()
        expansion.recover()
        sellersprite.recover()
        term_analysis.recover()
        hot_search_analysis.recover()
        traffic_auto_review.recover()
        library_relevance.recover()
        workflow_runner.recover()
        if agent_recovery is not None:
            agent_recovery.start()
        try:
            yield
        finally:
            if agent_recovery is not None:
                agent_recovery.stop()
            traffic_auto_review.close()
            library_relevance.close()
            source_exports.close()
            expansion.close()
            sellersprite.close()
            verification.close()
            workflow_runner.close()
            if agent_client is not None:
                agent_client.close()
            stack.close()
            # create_app owns this engine; release its pool on shutdown so restarts and
            # repeated app instances do not retain idle PostgreSQL client connections.
            engine.dispose()

    application = FastAPI(
        title="关键词库工作流 API",
        version="0.1.0",
        lifespan=lifespan,
        description="商品识别、词根推荐、搜索自然位置核验与八步编排；编排由 LangGraph 驱动。",
    )

    if not server_only:
        application.add_middleware(BrowserDeviceContextMiddleware)

    @application.middleware("http")
    async def request_id(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # 服务端生成独立请求 ID；参数校验失败时也能返回同一个排查标识。
        request.state.request_id = str(uuid4())
        result = await call_next(request)
        result.headers["X-Request-ID"] = str(request.state.request_id)
        return result

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # 只返回字段位置和校验原因，不回传完整输入或内部异常上下文。
        details = [
            {"field": ".".join(str(part) for part in error["loc"]), "reason": str(error["msg"])}
            for error in exc.errors()
        ]
        return error_response(request, 422, "VALIDATION_ERROR", "输入参数不符合要求", details)

    @application.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError) -> JSONResponse:
        return error_response(request, exc.status_code, exc.code, exc.message, [])

    @application.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        code = "NOT_FOUND" if exc.status_code == 404 else "HTTP_ERROR"
        response = error_response(request, exc.status_code, code, str(exc.detail), [])
        response.headers.update(exc.headers or {})
        return response

    # 资料服务与上传服务共用受控文件目录；测试数据库使用独立父目录。
    images = ImageService(repository, repository.storage_root / "uploads")
    asin_products = AsinProductService(repository, product_provider)
    application.include_router(
        build_router(repository, ProductMaterialsService(repository, images, asin_products))
    )
    application.include_router(build_model_config_router(repository.storage_root))
    application.include_router(build_image_router(images))
    application.include_router(build_browser_agent_router(browser_agent_status, server_only=server_only))
    if not server_only:
        application.include_router(build_browser_device_router(device_store, agent_channel))
        application.include_router(build_agent_channel_router(agent_channel, device_store))
        application.include_router(
            build_agent_pairing_router(
                pairing_codes,
                device_store,
                agent_channel,
                installer_path(repository.storage_root),
            )
        )
    # 配置在显式识别时加载，缺失或损坏不影响上传、健康检查和读取已有结果。
    understanding = ProductUnderstandingService(repository, images, model_client, asin_products)
    application.include_router(build_understanding_router(understanding))
    confirmations = ProductConfirmationService(repository, understanding)
    profiles = ProductProfileService(repository, confirmations, asin_products, model_client)
    application.include_router(build_product_profile_router(profiles))
    roots = RootService(repository, understanding, model_client)
    term_analysis = ProductTermAnalysisService(
        repository, roots, profiles, search_provider or AmazonSearch(), model_client
    )
    application.include_router(build_root_router(roots, term_analysis))
    application.include_router(build_product_term_analysis_router(term_analysis))
    verification = VerificationService(
        repository, roots, images, search_provider, model_client, auto_execute
    )
    application.include_router(build_verification_router(verification))
    aba = AbaService(
        repository, aba_client or AbaClient.from_directory(repository.storage_root / "aba")
    )
    application.include_router(build_aba_router(aba))
    from app.api.v1.source_exports import build_source_exports_router
    from app.services.source_exports import SourceExportService
    source_exports = SourceExportService(repository, aba.client, remote_sellersprite)
    aba.source_exports = source_exports
    application.include_router(build_source_exports_router(source_exports))
    hot_search_collection = HotSearchCollectionService(repository, aba)
    application.include_router(build_hot_search_collection_router(hot_search_collection))
    core = CoreService(
        repository,
        aba,
        model_client,
        hot_analysis_getter=lambda project, run, analysis_id: hot_search_analysis.get(
            project, run, analysis_id
        ),
    )
    application.include_router(build_core_router(core))
    hot_search_analysis = HotSearchCoreAnalysisService(
        repository, core, profiles, search_provider or AmazonSearch(), model_client
    )
    application.include_router(build_hot_search_core_analysis_router(hot_search_analysis))
    application.state.hot_search_core_analysis_service = hot_search_analysis
    competitors = CompetitorService(
        repository,
        core,
        asin_products.provider,
        search_provider,
        profiles,
        model_client,
        poe_downloader=remote_poe,
    )
    application.include_router(build_competitor_router(competitors))
    expansion = ExpansionService(repository, aba.client, core, competitors)
    expansion.source_exports = source_exports
    application.include_router(build_expansion_router(expansion))
    third_party = ThirdPartyService(repository, expansion)
    sellersprite = SellerspriteCollectionService(
        third_party, auto_execute=auto_execute, downloader=remote_sellersprite
    )
    source_exports.sellersprite = sellersprite.downloader
    sellersprite.source_exports = source_exports
    application.include_router(build_third_party_router(third_party, sellersprite))
    cleaning = CleaningService(repository, expansion, third_party, model_client)
    application.include_router(build_cleaning_router(cleaning))
    traffic = TrafficService(repository, cleaning)
    library = LibraryService(repository, traffic)
    library_relevance = LibraryRelevanceService(
        repository, library, profiles, search_provider or AmazonSearch(), model_client,
        auto_execute=auto_execute,
        collector_ready=(lambda: browser_agent_status.get(refresh=True).state == "online") if agent_client is not None else None,
    )
    browser_operation_store = BrowserOperationStore(engine)
    deps = WorkflowDeps(
        repository=repository,
        understanding=understanding,
        confirmation=confirmations,
        profiles=profiles,
        term_analysis=term_analysis,
        roots=roots,
        aba=aba,
        core=core,
        hot_search_core_analysis=hot_search_analysis,
        hot_search_collection=hot_search_collection,
        competitors=competitors,
        expansion=expansion,
        third_party=third_party,
        sellersprite=sellersprite,
        cleaning=cleaning,
        traffic=traffic,
        library=library,
        library_relevance=library_relevance,
        model=model_client,
        browser_operations=browser_operation_store,
        devices=None if server_only else device_store,
        channel=None if server_only else agent_channel,
        server_only=server_only,
    )
    traffic_auto_review = TrafficAutoReviewService(deps)
    application.include_router(build_traffic_router(traffic, traffic_auto_review))
    workflow_runner = WorkflowRunner(
        build_graph(deps, saver),
        workflow_store,
        operation_store=browser_operation_store,
        inline=workflow_inline,
    )
    library_relevance.use_executor(workflow_runner.executor)
    application.state.library_relevance_service = library_relevance
    application.include_router(build_library_router(library, library_relevance))
    if agent_client is not None:
        agent_recovery = AgentRecoveryScheduler(
            workflow_store,
            browser_operation_store,
            workflow_runner,
            lambda: browser_agent_status.get(refresh=True).state,
        )
    workflow_service = WorkflowService(deps, workflow_store, workflow_runner)
    application.include_router(build_workflow_router(workflow_service))
    application.include_router(build_research_router(repository))
    application.include_router(build_result_loading_router(repository, {
        'core': core, 'competitors': competitors, 'expansion': expansion,
        'third_party': third_party, 'cleaning': cleaning, 'traffic': traffic, 'library': library,
    }))
    install_result_cache(application, repository)
    application.state.verification_service = verification
    application.state.workflow_service = workflow_service
    return application


app = create_app()
