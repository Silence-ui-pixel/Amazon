"""Agent 操作处理器：把浏览器模块的调用包装成 JSON 信封返回给服务器。"""

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal
from uuid import UUID
from concurrent.futures import ThreadPoolExecutor, as_completed

from agent.runtime import AgentRuntime, SessionHandle
from app.adapters.browser_protocol import b64_files, encode_error
from app.domain.errors import DomainError
from app.domain.sellersprite import BrowserCollectionInput


def _read_evidence(handle: SessionHandle, *names: str) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for name in names:
        if not name:
            continue
        path = handle.evidence_dir / name
        if path.is_file():
            files[name] = path.read_bytes()
    return files


def open_session(runtime: AgentRuntime, marketplace: str, lanes: int = 1) -> dict[str, object]:
    try:
        return {"result": {"session_id": runtime.open_session(marketplace, lanes)}}
    except DomainError as error:
        return {"error": encode_error(error)}


def _search_on_lane(
    handle: SessionHandle, keyword: str, page_number: int, evidence_name: str
) -> dict[str, object]:
    lane = handle.acquire_lane()
    try:
        page = lane.session.search(keyword, page_number, evidence_name)
        files = _read_evidence(handle, page.screenshot, page.diagnostic)
        return {"keyword": keyword, "page": page.model_dump(), "files": b64_files(files), "error": None}
    except DomainError as error:
        files = _read_evidence(
            handle, getattr(error, "screenshot", ""), getattr(error, "diagnostic", "")
        )
        return {"keyword": keyword, "page": None, "files": b64_files(files), "error": encode_error(error)}
    finally:
        lane.lock.release()


def search(
    runtime: AgentRuntime, session_id: str, keyword: str, page_number: int, evidence_name: str
) -> dict[str, object]:
    def _do() -> dict[str, object]:
        handle = runtime.get(session_id)
        row = _search_on_lane(handle, keyword, page_number, evidence_name)
        if row["error"]:
            return {"error": row["error"], "files": row["files"]}
        return {"result": row["page"], "files": row["files"]}

    return runtime.run(_do)


def searches(runtime: AgentRuntime, session_id: str, requests: list[dict[str, object]], *, progress_id: str = "") -> dict[str, object]:
    """同步 Playwright 页面只在创建它的运行时线程上读取。"""
    def _do() -> dict[str, object]:
        handle = runtime.get(session_id)
        rows: list[dict[str, object]] = []
        files: dict[str, str] = {}
        for item in requests:
            row = _search_on_lane(handle, str(item["keyword"]), int(item["page_number"]), str(item["evidence_name"]))
            rows.append(row)
            files.update(cast_files(row.get("files")))
            if progress_id:
                from agent.status_ui import STATUS

                STATUS.progress(progress_id, {"result": rows}, f"已返回 {len(rows)}/{len(requests)} 个搜索页面")
        return {"result": rows, "files": files}

    try:
        return runtime.run(_do) if requests else {"result": [], "files": {}}
    except DomainError as error:
        return {"error": encode_error(error)}


def cast_files(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items()}


def product(runtime: AgentRuntime, session_id: str, asin: str) -> dict[str, object]:
    """兼容线上旧客户端：用当前搜索通道已连上的紫鸟打开商品页。"""
    def _do() -> dict[str, object]:
        handle = runtime.get(session_id)
        lane = handle.acquire_lane()
        try:
            snapshot = lane.session.product(asin)
            return {"result": snapshot.model_dump()}
        finally:
            lane.lock.release()

    try:
        return runtime.run(_do)
    except DomainError as error:
        return {"error": encode_error(error)}


def products(runtime: AgentRuntime, session_id: str, asins: list[str], *, progress_id: str = "") -> dict[str, object]:
    """同批最多三路详情采集；每路自行创建连接，不跨线程传递搜索 Page。"""

    def _do() -> list[tuple[str, object, Exception | None]]:
        handle = runtime.get(session_id)
        lane = handle.acquire_lane()
        collected: dict[int, tuple[str, object, Exception | None]] = {}
        try:
            # 只在会话所属线程读取不可变配置。搜索标签本身不交给详情工作线程。
            delivery_ready = bool(getattr(lane.session, "delivery_ready", False))
            with ThreadPoolExecutor(max_workers=min(3, len(asins)), thread_name_prefix="agent-product") as pool:
                pending = {
                    pool.submit(runtime.products.fetch, handle.marketplace, asin,
                                delivery_ready=delivery_ready): (index, asin)
                    for index, asin in enumerate(asins)
                }
                for future in as_completed(pending):
                    index, asin = pending[future]
                    try:
                        collected[index] = (asin, future.result(), None)
                    except Exception as error:
                        collected[index] = (asin, None, error)
                    if progress_id:
                        from agent.status_ui import STATUS

                        STATUS.progress(progress_id, {"result": [
                            {"asin": item_asin, "product": item.model_dump() if item is not None else None,
                             "error": {"message": str(item_error)[:500]} if item_error else None}
                            for item_asin, item, item_error in [collected[i] for i in sorted(collected)]
                        ]}, f"三路并发采集：已读取 {len(collected)}/{len(asins)} 个商品详情")
        finally:
            lane.lock.release()
        return [collected[index] for index in range(len(asins))]

    try:
        results: list[tuple[str, object, Exception | None]] = []
        if asins:
            results = runtime.run(_do)
        return {
            "result": [
                {
                    "asin": asin,
                    "product": product.model_dump() if product is not None else None,
                    "error": encode_error(error) if isinstance(error, DomainError) else (
                        {"code": "PRODUCT_READ_FAILED", "message": str(error)[:500]} if error else None
                    ),
                }
                for asin, product, error in results
            ]
        }
    except DomainError as error:
        return {"error": encode_error(error)}


def close_session(runtime: AgentRuntime, session_id: str) -> dict[str, object]:
    try:
        runtime.close_session(session_id)
        return {"result": {"ok": True}}
    except DomainError as error:
        return {"error": encode_error(error)}


def product_one_shot(runtime: AgentRuntime, marketplace: str, asin: str) -> dict[str, object]:
    def _do() -> dict[str, object]:
        snapshot = runtime.products.fetch_isolated(marketplace, asin)
        return {"result": snapshot.model_dump()}

    try:
        return runtime.run(_do)
    except DomainError as error:
        return {"error": encode_error(error)}


def aba_report(
    runtime: AgentRuntime,
    query: str,
    week_end: str,
    *,
    country: str,
    max_rows: int,
    query_mode: Literal["root", "asin", "product_term"],
    rank_limit: int | None = None,
) -> dict[str, object]:
    def _do() -> dict[str, object]:
        report = runtime.aba.fetch(
            query, week_end, country=country, max_rows=max_rows, query_mode=query_mode, rank_limit=rank_limit
        )
        # raw_rows 被排除在序列化之外，但服务器要存原始证据，单独随信封回传。
        return {"result": report.model_dump(), "raw_rows": report.raw_rows}

    try:
        return runtime.run(_do)
    except DomainError as error:
        return {"error": encode_error(error)}


def aba_status(runtime: AgentRuntime) -> dict[str, object]:
    try:
        return {"result": runtime.aba.status().model_dump()}
    except DomainError as error:
        return {"error": encode_error(error)}


def sellersprite_export(
    runtime: AgentRuntime, request: BrowserCollectionInput
) -> dict[str, object]:
    def _do() -> dict[str, object]:
        with TemporaryDirectory(prefix="keyword-agent-sellersprite-") as temporary:
            target = Path(temporary) / "original.xlsx"
            filename, raw, count = runtime.sellersprite.download(request, target)
            return {
                "result": {"filename": filename, "count": count},
                "files": b64_files({filename: raw}),
            }

    try:
        return runtime.run(_do)
    except DomainError as error:
        return {"error": encode_error(error)}


def poe_intents(runtime: AgentRuntime, keyword: str) -> dict[str, object]:
    try:
        return runtime.run(lambda: {"result": [item.model_dump(mode="json") for item in runtime.poe.intents(keyword)]})
    except DomainError as error:
        return {"error": encode_error(error)}


def poe_download(runtime: AgentRuntime, run_id: str, keyword: str, intent_ref: str = "") -> dict[str, object]:
    def _do() -> dict[str, object]:
        archive = runtime.poe.download(UUID(run_id), keyword, intent_ref) if intent_ref else runtime.poe.download(UUID(run_id), keyword)
        raw = archive.path.read_bytes()
        return {
            "result": {"filename": archive.original_filename, "source_url": archive.source_url,
                "export_metadata": archive.export_metadata},
            "files": b64_files({archive.original_filename: raw}),
        }

    try:
        return runtime.run(_do)
    except DomainError as error:
        return {"error": encode_error(error)}


def platform_original(runtime: AgentRuntime, request: dict, *, sellersprite: bool = False) -> dict[str, object]:
    """Only this operation returns actual files received from the platform download action."""
    def collect():
        with TemporaryDirectory(prefix='keyword-agent-original-') as directory:
            target = Path(directory) / 'download.xlsx'
            if sellersprite:
                from app.adapters.sellersprite_browser import SellerspriteBrowserDownloader
                downloader = SellerspriteBrowserDownloader(runtime.sellersprite.port, directory=runtime.data / 'sellersprite')
                name, raw, count = downloader.download_original(BrowserCollectionInput.model_validate(request), target)
            else:
                from app.adapters.aba import AbaClient
                name, raw = AbaClient.from_directory(runtime.data / 'aba').download_original(request, target)
                count = 0
            return {'result': {'filename': name, 'count': count}, 'files': b64_files({name: raw})}
    try:
        # 下载使用独立适配器与独占页面，不占商品采集的串行线程。
        # 服务器下载队列仍保证同一设备一次只创建一个平台导出。
        return collect()
    except DomainError as error:
        return {'error': encode_error(error)}
    except Exception:
        import logging
        logging.getLogger(__name__).exception('platform original download failed')
        return {'error': encode_error(DomainError(503, 'AGENT_EXPORT_FAILED', '平台原件下载异常，请查看采集器日志后重试'))}
