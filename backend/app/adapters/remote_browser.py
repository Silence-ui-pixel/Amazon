"""服务器侧的远程浏览器客户端：把浏览器操作转发给本地 Agent。

每个类都实现现有的 Protocol（SearchProvider / ProductProvider / AbaProvider /
SellerspriteDownloader / PoeDownloader），因此业务服务无需知道浏览器在本地还是远程。
"""

from collections.abc import Callable, Generator
from concurrent.futures import Executor, Future
from contextlib import AbstractContextManager, contextmanager, suppress
from contextvars import ContextVar, copy_context
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from time import monotonic
from typing import Any, Literal, Protocol, cast
from uuid import UUID

import httpx

from app.adapters.browser_protocol import decode_files, raise_error, write_files
from app.adapters.poe_downloads import PoeArchive, PoeDownloadStore
from app.domain.aba import AbaConnectionStatus, AbaQueryReport
from app.domain.asin_product import ProductSnapshot
from app.domain.competitors import PoeIntentCandidate
from app.domain.errors import DomainError
from app.domain.sellersprite import BrowserCollectionInput
from app.domain.verification import SearchPage
from app.services.amazon_search import SearchSession
from app.services.product_term_collection import SEARCH_CONCURRENCY

_CURRENT_OPERATION_ID: ContextVar[str | None] = ContextVar(
    "keyword_browser_operation_id", default=None
)
_CURRENT_DEVICE_ID: ContextVar[str | None] = ContextVar(
    "keyword_browser_device_id", default=None
)

UNBOUND_MESSAGE = "当前任务未绑定浏览器设备，请先在访问者电脑上完成 Agent 配对"
# 自动绑定设备的缓存时长：短时间内保持同一台，避免节点内多次调用中途换设备。
AUTO_BIND_TTL_SECONDS = 30.0


@contextmanager
def browser_operation_context(operation_id: str) -> Generator[None, None, None]:
    """让当前工作流节点内的所有远程调用自动携带同一个持久操作 ID。"""
    token = _CURRENT_OPERATION_ID.set(operation_id)
    try:
        yield
    finally:
        _CURRENT_OPERATION_ID.reset(token)


@contextmanager
def browser_device_context(device_id: str | None) -> Generator[None, None, None]:
    """把当前任务绑定的访问者设备带到该节点内的所有远程调用（含后台线程）。"""
    token = _CURRENT_DEVICE_ID.set(device_id or None)
    try:
        yield
    finally:
        _CURRENT_DEVICE_ID.reset(token)


def current_browser_operation_id() -> str:
    return _CURRENT_OPERATION_ID.get() or ""


def current_browser_device_id() -> str:
    return _CURRENT_DEVICE_ID.get() or ""


def submit_with_browser_operation_context[**P, R](
    executor: Executor, fn: Callable[P, R], *args: P.args, **kwargs: P.kwargs
) -> Future[R]:
    """Submit background work with the operation context captured at creation time."""
    context = copy_context()
    return executor.submit(context.run, fn, *args, **kwargs)


def validate_agent_settings(
    base_url: str | None, token: str | None
) -> tuple[str | None, str | None]:
    """校验服务器侧远程 Agent URL/Token 必须成对且 Token 足够强。"""
    normalized_url = (base_url or "").strip() or None
    normalized_token = (token or "").strip() or None
    if bool(normalized_url) != bool(normalized_token):
        raise ValueError("KEYWORD_BROWSER_AGENT_URL 与 KEYWORD_BROWSER_AGENT_TOKEN 必须同时配置")
    if normalized_token is not None and len(normalized_token) < 32:
        raise ValueError("浏览器 Agent Token 至少 32 个字符")
    return normalized_url, normalized_token


@dataclass(frozen=True)
class AgentEndpoint:
    """一台访问者电脑上的 Agent 地址与凭据。"""

    base_url: str
    token: str | None = None


class AgentChannel(Protocol):
    """采集端反向长连接：客户端主动连服务器，不需要公网地址。"""

    def is_connected(self, device_id: str) -> bool: ...

    def connected_devices(self) -> list[str]: ...

    def call(
        self,
        device_id: str,
        method: str,
        path: str,
        body: dict[str, object] | None,
        *,
        operation_id: str = "",
        timeout: float = 600.0,
    ) -> dict[str, Any]: ...


def auto_bind_device_id(channel: AgentChannel | None) -> str:
    """未指定设备时，若恰好只有一台采集端在线，就自动用它。

    这是兜底而不是猜测：只有一台在线时才存在唯一答案，多台在线一律返回空，
    宁可让用户显式选择，也绝不能把任务发到别人的紫鸟上。
    """
    if channel is None:
        return ""
    try:
        connected = channel.connected_devices()
    except Exception:  # noqa: BLE001 - 通道状态异常时按"没有唯一设备"处理
        return ""
    return connected[0] if len(connected) == 1 else ""


class AgentClient:
    """远程 Agent 客户端。

    默认地址由服务器环境变量给出（单操作员过渡）；配置 resolver 后，每个请求都会重新
    解析当前任务绑定的访问者设备，因此同一个客户端可以服务多台设备，绝不串号。
    """

    def __init__(
        self,
        base_url: str | None = None,
        *,
        token: str | None = None,
        resolver: Callable[[], AgentEndpoint | None] | None = None,
        channel: AgentChannel | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        # 卖家精灵导出最长约 6 分钟，POE/ABA 也可能分钟级，连接超时单独收紧。
        self._client = httpx.Client(
            timeout=httpx.Timeout(600.0, connect=15.0),
            trust_env=False,
            transport=transport,
        )
        self._default = (
            AgentEndpoint(base_url.rstrip("/"), token) if base_url and base_url.strip() else None
        )
        self._resolver = resolver
        self._channel = channel
        # 自动绑定缓存：短时间内保持同一台设备，避免节点内多次调用中途换人。
        self._auto_device_id = ""
        self._auto_device_at = 0.0

    def close(self) -> None:
        self._client.close()

    def _resolve_auto_device(self) -> str:
        """没有显式绑定设备时，解析唯一在线的采集端。"""
        if self._channel is None:
            return ""
        # 检查必须先于短期缓存和旧版默认地址：多设备时不能沿用另一访问者的采集端。
        if len(self._channel.connected_devices()) > 1:
            raise DomainError(409, "BROWSER_DEVICE_UNBOUND", UNBOUND_MESSAGE)
        stamp = monotonic()
        cached = self._auto_device_id
        if (
            cached
            and stamp - self._auto_device_at < AUTO_BIND_TTL_SECONDS
            and self._channel.is_connected(cached)
        ):
            return cached
        device_id = auto_bind_device_id(self._channel)
        self._auto_device_id = device_id
        self._auto_device_at = stamp
        return device_id

    def _endpoint(self) -> AgentEndpoint:
        resolved = self._resolver() if self._resolver is not None else None
        endpoint = resolved or self._default
        if endpoint is None:
            # 绑定了设备却连不上：说明采集端没连上来，而不是"没绑定"。
            if _CURRENT_DEVICE_ID.get():
                raise DomainError(
                    503, "AGENT_CHANNEL_OFFLINE", "该设备上的采集端未连接，请先启动客户端"
                )
            raise DomainError(409, "BROWSER_DEVICE_UNBOUND", UNBOUND_MESSAGE)
        return endpoint

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, object] | None,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        effective_operation_id = operation_id or _CURRENT_OPERATION_ID.get()
        device_id = _CURRENT_DEVICE_ID.get() or ""
        if not device_id:
            # 没有绑定设备时兜底：只有一台采集端在线就用它。
            device_id = self._resolve_auto_device()
        # 优先走反向长连接：客户端主动连服务器，不需要公网地址或隧道。
        if self._channel is not None and device_id and self._channel.is_connected(device_id):
            channel_payload = self._channel.call(
                device_id,
                method,
                path,
                json_body,
                operation_id=(effective_operation_id or "").strip(),
            )
            if not isinstance(channel_payload, dict):
                raise DomainError(
                    502, "AGENT_INVALID_RESPONSE", "浏览器 Agent 返回结构异常"
                ) from None
            return cast(dict[str, Any], channel_payload)
        if device_id and self._resolver is not None and self._resolver() is None:
            # 绑定的设备只有长连接通道，但它此刻没连上来：直接报离线，
            # 绝不回退到服务器默认采集端（那是别人的紫鸟，串号比失败更糟）。
            raise DomainError(
                503, "AGENT_CHANNEL_OFFLINE", "该设备上的采集端未连接，请先启动客户端"
            )
        endpoint = self._endpoint()
        headers: dict[str, str] = {}
        if endpoint.token:
            headers["Authorization"] = f"Bearer {endpoint.token}"
        effective_operation_id = operation_id or _CURRENT_OPERATION_ID.get()
        if effective_operation_id and effective_operation_id.strip():
            headers["X-Keyword-Operation-ID"] = effective_operation_id.strip()
        try:
            response = self._client.request(
                method,
                endpoint.base_url + path,
                json=json_body,
                headers=headers or None,
            )
        except httpx.HTTPError as error:
            raise DomainError(
                503, "AGENT_UNAVAILABLE", f"浏览器 Agent 不可达（{type(error).__name__}）"
            ) from None
        try:
            payload = response.json()
        except ValueError:
            raise DomainError(502, "AGENT_INVALID_RESPONSE", "浏览器 Agent 返回非 JSON") from None
        if not isinstance(payload, dict):
            raise DomainError(502, "AGENT_INVALID_RESPONSE", "浏览器 Agent 返回结构异常") from None
        payload = cast(dict[str, Any], payload)
        if response.status_code >= 400 and not payload.get("error"):
            raise DomainError(
                502, "AGENT_HTTP_ERROR", f"浏览器 Agent 返回 HTTP {response.status_code}"
            ) from None
        return payload

    def raw(
        self,
        method: str,
        path: str,
        body: dict[str, object] | None = None,
        *,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        """返回信封但不处理 error 字段，供需要先落盘失败证据的调用方使用。"""
        return self._request(method, path, json_body=body, operation_id=operation_id)

    def call(
        self,
        method: str,
        path: str,
        body: dict[str, object] | None = None,
        *,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        payload = self._request(method, path, json_body=body, operation_id=operation_id)
        if payload.get("error"):
            raise_error(cast(dict[str, Any], payload["error"]))
        return payload


class RemoteSearchSession:
    def __init__(
        self,
        client: AgentClient,
        session_id: str,
        evidence_dir: Path,
        marketplace: str,
        lanes: int = SEARCH_CONCURRENCY,
    ) -> None:
        self._client = client
        self._session_id = session_id
        self._evidence_dir = evidence_dir
        self._marketplace = marketplace
        self._lanes = max(1, min(4, lanes))
        self._session_lock = Lock()

    def _reopen(self, expected_session_id: str | None = None) -> str:
        with self._session_lock:
            if expected_session_id is not None and self._session_id != expected_session_id:
                return self._session_id
            payload = self._client.call(
                "POST",
                "/v1/session",
                {"marketplace": self._marketplace, "lanes": self._lanes},
            )
            self._session_id = str(payload["result"]["session_id"])
            return self._session_id

    def _search_payload(
        self, keyword: str, page_number: int, evidence_name: str, session_id: str
    ) -> dict[str, Any]:
        payload = self._client.raw(
            "POST",
            f"/v1/session/{session_id}/search",
            {"keyword": keyword, "page_number": page_number, "evidence_name": evidence_name},
        )
        write_files(decode_files(payload), self._evidence_dir)
        if payload.get("error"):
            raise_error(cast(dict[str, Any], payload["error"]))
        return payload

    def search(self, keyword: str, page_number: int, evidence_name: str) -> SearchPage:
        session_id = self._session_id
        try:
            payload = self._search_payload(keyword, page_number, evidence_name, session_id)
        except DomainError as error:
            if error.code != "AGENT_SESSION_NOT_FOUND":
                raise
            session_id = self._reopen(session_id)
            payload = self._search_payload(keyword, page_number, evidence_name, session_id)
        return SearchPage.model_validate(payload["result"])

    def product(self, asin: str) -> ProductSnapshot:
        session_id = self._session_id
        try:
            payload = self._client.call(
                "POST", f"/v1/session/{session_id}/product", {"asin": asin}
            )
        except DomainError as error:
            if error.code != "AGENT_SESSION_NOT_FOUND":
                raise
            session_id = self._reopen(session_id)
            payload = self._client.call(
                "POST", f"/v1/session/{session_id}/product", {"asin": asin}
            )
        return ProductSnapshot.model_validate(payload["result"])

    def _products_payload(self, asins: list[str], session_id: str) -> dict[str, Any]:
        return self._client.call(
            "POST", f"/v1/session/{session_id}/products", {"asins": asins}
        )

    def products(
        self, asins: list[str], *, _recover_closed: bool = True
    ) -> list[tuple[str, ProductSnapshot | None, Exception | None]]:
        """Keep successful rows and retry closed-session failures at most once."""
        if not asins:
            return []
        session_id = self._session_id
        try:
            payload = self._products_payload(asins, session_id)
        except DomainError as error:
            if error.code != "AGENT_SESSION_NOT_FOUND" or not _recover_closed:
                raise
            session_id = self._reopen(session_id)
            _recover_closed = False
            payload = self._products_payload(asins, session_id)

        raw_rows = payload.get("result")
        if not isinstance(raw_rows, list):
            raise DomainError(502, "AGENT_INVALID_RESPONSE", "浏览器 Agent 批量商品结果异常")

        requested = set(asins)
        results: dict[str, tuple[str, ProductSnapshot | None, Exception | None]] = {}
        for raw_row in raw_rows:
            if not isinstance(raw_row, dict):
                raise DomainError(502, "AGENT_INVALID_RESPONSE", "浏览器 Agent 批量商品行异常")
            row = cast(dict[str, Any], raw_row)
            asin = row.get("asin")
            if not isinstance(asin, str) or asin not in requested or asin in results:
                raise DomainError(502, "AGENT_INVALID_RESPONSE", "浏览器 Agent 批量商品 ASIN 异常")
            raw_product = row.get("product")
            raw_error = row.get("error")
            if isinstance(raw_product, dict):
                try:
                    product = ProductSnapshot.model_validate(raw_product)
                except ValueError:
                    error = DomainError(
                        502, "AGENT_INVALID_RESPONSE", "浏览器 Agent 商品详情结构异常"
                    )
                    results[asin] = (asin, None, error)
                    continue
                if product.asin != asin:
                    error = DomainError(
                        502, "AGENT_INVALID_RESPONSE", "浏览器 Agent 商品 ASIN 不一致"
                    )
                    results[asin] = (asin, None, error)
                    continue
                results[asin] = (asin, product, None)
                continue
            if isinstance(raw_error, dict):
                try:
                    raise_error(cast(dict[str, Any], raw_error))
                except DomainError as error:
                    results[asin] = (asin, None, error)
                    continue
            results[asin] = (
                asin,
                None,
                DomainError(502, "AGENT_INVALID_RESPONSE", "浏览器 Agent 商品结果缺失"),
            )

        retry = [
            asin for asin, (_, product, error) in results.items()
            if product is None and isinstance(error, DomainError)
            and (error.code == "AGENT_SESSION_NOT_FOUND" or (
                error.code == "PRODUCT_READ_FAILED"
                and "target page, context or browser has been closed" in error.message.casefold()
            ))
        ]
        if retry and _recover_closed:
            try:
                self._reopen(session_id)
                for row in self.products(retry, _recover_closed=False):
                    results[row[0]] = row
            except DomainError as error:
                for asin in retry:
                    results[asin] = (asin, None, error)

        return [
            results.get(
                asin,
                (
                    asin,
                    None,
                    DomainError(502, "AGENT_INVALID_RESPONSE", "浏览器 Agent 未返回该商品"),
                ),
            )
            for asin in asins
        ]

    def __enter__(self) -> "RemoteSearchSession":
        return self

    def __exit__(self, *exc: object) -> bool:
        # 会话已失效或网络中断时不再抛出，避免覆盖业务异常。
        with suppress(DomainError):
            self._client.call("DELETE", f"/v1/session/{self._session_id}")
        return False


class RemoteSearchProvider:
    supports_parallel_terms = True

    def __init__(self, client: AgentClient) -> None:
        self._client = client

    def open(self, marketplace: str, evidence_dir: Path) -> AbstractContextManager[SearchSession]:
        lanes = max(1, min(4, SEARCH_CONCURRENCY))
        payload = self._client.call(
            "POST", "/v1/session", {"marketplace": marketplace, "lanes": lanes}
        )
        session_id = str(payload["result"]["session_id"])
        return RemoteSearchSession(self._client, session_id, evidence_dir, marketplace, lanes)


class RemoteProductProvider:
    def __init__(self, client: AgentClient) -> None:
        self._client = client

    def fetch(self, marketplace: str, asin: str) -> ProductSnapshot:
        payload = self._client.call(
            "POST", "/v1/product", {"marketplace": marketplace, "asin": asin}
        )
        return ProductSnapshot.model_validate(payload["result"])

    def fetch_many(
        self, marketplace: str, asins: list[str]
    ) -> list[tuple[str, ProductSnapshot | None, Exception | None]]:
        if not asins:
            return []
        results: list[tuple[str, ProductSnapshot | None, Exception | None]] = []
        with (
            TemporaryDirectory(prefix="keyword-competitor-details-") as temporary,
            RemoteSearchProvider(self._client).open(marketplace, Path(temporary)) as session,
        ):
            # 采集器串行读取详情；每批五条避免超过长连接单请求 600 秒时限。
            for offset in range(0, len(asins), 5):
                results.extend(session.products(asins[offset : offset + 5]))
        return results


class RemoteAbaClient:
    """实现 AbaProvider；凭据/直连/兜底都在 Agent 侧，服务器只拿结果与原始行。"""

    def __init__(self, client: AgentClient) -> None:
        self._client = client

    def fetch(
        self,
        query: str,
        week_end: str,
        *,
        country: str = "us",
        max_rows: int = 1000,
        query_mode: Literal["root", "asin", "product_term"] = "root",
        rank_limit: int | None = None,
        on_progress: Callable[[AbaQueryReport], None] | None = None,
    ) -> AbaQueryReport:
        payload = self._client.call(
            "POST",
            "/v1/aba/report",
            {
                "query": query,
                "week_end": week_end,
                "country": country,
                "max_rows": max_rows,
                "query_mode": query_mode,
                "rank_limit": rank_limit,
            },
        )
        report = AbaQueryReport.model_validate(payload["result"])
        report.raw_rows = cast(list[dict[str, object]], payload.get("raw_rows", []))
        # 远程无法流式回报分页进度；最终结果仍由调用方一次性保存。
        return report

    def status(self) -> AbaConnectionStatus:
        payload = self._client.call("GET", "/v1/aba/status")
        return AbaConnectionStatus.model_validate(payload["result"])

    def download_original(
        self, request: object, target: Path
    ) -> tuple[str, bytes]:
        """ABA 平台原件（周报 xlsx）：转给采集器的 /v1/aba/download-original。

        之前这个类没有该方法，工作流「商品词采集与表格同步」会误报
        「采集器版本不支持 ABA 平台原件下载」——实际是服务端能力缺口。
        """
        body = request if isinstance(request, dict) else cast(dict[str, Any], request)
        payload = self._client.call("POST", "/v1/aba/download-original", dict(body))
        result = cast(dict[str, Any], payload.get("result") or {})
        filename = str(result.get("filename") or "ABA-original.xlsx")
        raw = decode_files(payload).get(filename)
        if raw is None:
            raise DomainError(502, "AGENT_FILE_MISSING", "采集器未返回 ABA 平台原件")
        target.write_bytes(raw)
        return filename, raw


class RemoteSellerspriteDownloader:
    def __init__(self, client: AgentClient) -> None:
        self._client = client

    def download(self, request: BrowserCollectionInput, target: Path) -> tuple[str, bytes, int]:
        payload = self._client.call("POST", "/v1/sellersprite/export", request.model_dump())
        result = cast(dict[str, Any], payload["result"])
        filename = str(result["filename"])
        raw = decode_files(payload).get(filename)
        if raw is None:
            raise DomainError(502, "AGENT_FILE_MISSING", "浏览器 Agent 未返回卖家精灵原件")
        return filename, raw, int(result["count"])


class RemotePoeDownloader:
    def __init__(self, client: AgentClient, store: PoeDownloadStore) -> None:
        self._client = client
        self._store = store

    def intents(self, keyword: str) -> list[PoeIntentCandidate]:
        payload = self._client.call("POST", "/v1/poe/intents", {"keyword": keyword})
        try:
            return [PoeIntentCandidate.model_validate(item) for item in payload["result"]]
        except (KeyError, TypeError, ValueError):
            raise DomainError(502, "POE_INTENT_RESULTS_INVALID", "采集器未返回有效购买意图") from None

    def download(self, run_id: UUID, keyword: str, intent_ref: str = "") -> PoeArchive:
        body: dict[str, object] = {"run_id": str(run_id), "keyword": keyword}
        if intent_ref:
            body["intent_ref"] = intent_ref
        payload = self._client.call(
            "POST", "/v1/poe/download", body
        )
        result = cast(dict[str, Any], payload["result"])
        filename = str(result["filename"])
        source_url = str(result["source_url"])
        raw = decode_files(payload).get(filename)
        if raw is None:
            raise DomainError(502, "AGENT_FILE_MISSING", "浏览器 Agent 未返回商机 CSV")
        with TemporaryDirectory(prefix="keyword-remote-poe-") as temporary:
            source = Path(temporary) / "download.csv"
            source.write_bytes(raw)
            metadata = result.get("export_metadata")
            if isinstance(metadata, dict) and metadata:
                return self._store.archive(source, run_id, keyword, filename, source_url,
                    export_metadata=cast(dict[str, object], metadata))
            return self._store.archive(source, run_id, keyword, filename, source_url)
