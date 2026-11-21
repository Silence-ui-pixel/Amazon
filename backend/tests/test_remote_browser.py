"""服务器远程客户端：把 Agent 信封还原成本地同构对象并落盘证据。"""

import base64
import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import httpx
import pytest

from app.adapters.poe_downloads import PoeArchive, PoeDownloadStore
from app.adapters.remote_browser import (
    AgentClient,
    RemoteAbaClient,
    RemotePoeDownloader,
    RemoteProductProvider,
    RemoteSearchProvider,
    RemoteSellerspriteDownloader,
    browser_operation_context,
    submit_with_browser_operation_context,
    validate_agent_settings,
)
from app.domain.errors import DomainError
from app.domain.sellersprite import BrowserCollectionInput
from app.services.search_policy import SearchFailure

Handler = Callable[[httpx.Request], httpx.Response]


def _client(handler: Handler) -> AgentClient:
    return AgentClient(
        "http://agent",
        token="agent-token-0123456789abcdef-0123456789",
        transport=httpx.MockTransport(handler),
    )


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def test_agent_client_sends_bearer_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"result": {"authorization": request.headers.get("Authorization")}},
        )

    payload = _client(handler).call("GET", "/v1/aba/status")
    assert payload["result"] == {"authorization": "Bearer agent-token-0123456789abcdef-0123456789"}


def test_agent_client_sends_operation_id_only_when_provided() -> None:
    received: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received.append(request.headers.get("X-Keyword-Operation-ID"))
        return httpx.Response(200, json={"result": {"ok": True}})

    client = _client(handler)
    client.call("GET", "/v1/status", operation_id="operation-123")
    client.call("GET", "/v1/status")

    assert received == ["operation-123", None]


def test_agent_client_inherits_operation_id_from_workflow_context() -> None:
    """抓住业务适配器忘记逐层传 operation_id，导致 Agent 无法关联断点记录。"""
    received: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received.append(request.headers.get("X-Keyword-Operation-ID"))
        return httpx.Response(200, json={"result": {"ok": True}})

    client = _client(handler)
    with browser_operation_context("workflow-operation-456"):
        client.call("GET", "/v1/status")
    client.call("GET", "/v1/status")

    assert received == ["workflow-operation-456", None]


def test_worker_submission_preserves_browser_operation_context() -> None:
    received: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received.append(request.headers.get("X-Keyword-Operation-ID"))
        return httpx.Response(200, json={"result": {"ok": True}})

    client = _client(handler)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with browser_operation_context("workflow-operation-worker"):
            future = submit_with_browser_operation_context(
                executor, client.call, "GET", "/v1/status"
            )
        future.result(timeout=2)

    assert received == ["workflow-operation-worker"]


@pytest.mark.parametrize(
    ("url", "token"),
    [("http://agent:7100", ""), ("", "agent-token-0123456789abcdef-0123456789")],
)
def test_remote_agent_url_and_token_must_be_configured_together(url: str, token: str) -> None:
    with pytest.raises(ValueError, match="同时配置"):
        validate_agent_settings(url, token)


def test_remote_agent_token_must_be_strong() -> None:
    with pytest.raises(ValueError, match="至少 32"):
        validate_agent_settings("http://agent:7100", "short")


def test_search_session_writes_evidence_and_returns_page(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/session":
            return httpx.Response(200, json={"result": {"session_id": "abc"}})
        if request.url.path == "/v1/session/abc/search":
            return httpx.Response(
                200,
                json={
                    "result": {
                        "region": "New York 10001",
                        "cards": [],
                        "next_page": False,
                        "url": "https://www.amazon.com/s",
                        "screenshot": "s.png",
                        "diagnostic": "s.json",
                    },
                    "files": {
                        "s.png": _b64(b"PNGDATA"),
                        "s.json": _b64(b'{"ok": 1}'),
                    },
                },
            )
        return httpx.Response(200, json={"result": {"ok": True}})

    provider = RemoteSearchProvider(_client(handler))
    with provider.open("US", tmp_path) as session:
        page = session.search("rack", 1, "ev")
    assert page.region == "New York 10001"
    assert page.screenshot == "s.png"
    assert (tmp_path / "s.png").read_bytes() == b"PNGDATA"
    assert (tmp_path / "s.json").read_bytes() == b'{"ok": 1}'


def test_search_failure_raises_after_writing_evidence(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/session":
            return httpx.Response(200, json={"result": {"session_id": "abc"}})
        return httpx.Response(
            200,
            json={
                "error": {
                    "status_code": 502,
                    "code": "SEARCH_ACCESS_CHECK",
                    "message": "需要人工确认",
                    "stage": "results",
                    "screenshot": "f.png",
                    "diagnostic": "f.json",
                },
                "files": {"f.png": _b64(b"FAIL"), "f.json": _b64(b"{}")},
            },
        )

    provider = RemoteSearchProvider(_client(handler))
    with provider.open("US", tmp_path) as session, pytest.raises(SearchFailure) as caught:
        session.search("rack", 1, "ev")
    assert caught.value.screenshot == "f.png"
    assert (tmp_path / "f.png").read_bytes() == b"FAIL"


def test_search_session_reopens_once_when_agent_session_is_lost(tmp_path: Path) -> None:
    opens = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal opens
        if request.url.path == "/v1/session":
            opens += 1
            return httpx.Response(200, json={"result": {"session_id": f"s{opens}"}})
        if request.url.path == "/v1/session/s1/search":
            return httpx.Response(
                200,
                json={
                    "error": {
                        "status_code": 404,
                        "code": "AGENT_SESSION_NOT_FOUND",
                        "message": "session expired",
                    }
                },
            )
        if request.url.path == "/v1/session/s2/search":
            return httpx.Response(
                200,
                json={
                    "result": {
                        "region": "New York 10001",
                        "cards": [],
                        "next_page": False,
                    }
                },
            )
        return httpx.Response(200, json={"result": {"ok": True}})

    with RemoteSearchProvider(_client(handler)).open("US", tmp_path) as session:
        page = session.search("rack", 1, "evidence")

    assert page.region == "New York 10001"
    assert opens == 2


def test_product_provider_returns_snapshot() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"result": {"asin": "B0TEST1234", "title": "Widget"}},
        )

    snapshot = RemoteProductProvider(_client(handler)).fetch("US", "B0TEST1234")
    assert snapshot.asin == "B0TEST1234"
    assert snapshot.title == "Widget"


def test_search_session_fetches_products_in_one_batch_and_preserves_input_order(
    tmp_path: Path,
) -> None:
    requests: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        requests.append((request.url.path, body))
        if request.url.path == "/v1/session":
            return httpx.Response(200, json={"result": {"session_id": "abc"}})
        if request.url.path == "/v1/session/abc/products":
            return httpx.Response(
                200,
                json={
                    "result": [
                        {
                            "asin": "B0SECOND02",
                            "product": {"asin": "B0SECOND02", "title": "Second"},
                            "error": None,
                        },
                        {
                            "asin": "B0FIRST001",
                            "product": {"asin": "B0FIRST001", "title": "First"},
                            "error": None,
                        },
                    ]
                },
            )
        return httpx.Response(200, json={"result": {"ok": True}})

    with RemoteSearchProvider(_client(handler)).open("US", tmp_path) as session:
        results = session.products(["B0FIRST001", "B0SECOND02"])

    assert [(asin, snapshot.title if snapshot else None, error) for asin, snapshot, error in results] == [
        ("B0FIRST001", "First", None),
        ("B0SECOND02", "Second", None),
    ]
    assert requests[1] == (
        "/v1/session/abc/products",
        {"asins": ["B0FIRST001", "B0SECOND02"]},
    )
    assert [path for path, _ in requests].count("/v1/session/abc/product") == 0


def test_search_session_keeps_per_product_error_inside_batch(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/session":
            return httpx.Response(200, json={"result": {"session_id": "abc"}})
        if request.url.path == "/v1/session/abc/products":
            return httpx.Response(
                200,
                json={
                    "result": [
                        {
                            "asin": "B0GOOD0001",
                            "product": {"asin": "B0GOOD0001", "title": "Good"},
                            "error": None,
                        },
                        {
                            "asin": "B0FAILED02",
                            "product": None,
                            "error": {
                                "status_code": 503,
                                "code": "AGENT_BROWSER_ERROR",
                                "message": "detail failed",
                            },
                        },
                    ]
                },
            )
        return httpx.Response(200, json={"result": {"ok": True}})

    with RemoteSearchProvider(_client(handler)).open("US", tmp_path) as session:
        results = session.products(["B0GOOD0001", "B0FAILED02"])

    assert results[0][1] is not None
    assert results[0][2] is None
    assert results[1][1] is None
    assert isinstance(results[1][2], DomainError)
    assert results[1][2].code == "AGENT_BROWSER_ERROR"


def test_aba_client_reconstructs_raw_rows() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "result": {
                    "query": "rack",
                    "query_mode": "root",
                    "total": 1,
                    "fetched": 1,
                    "pages": 1,
                    "truncated": False,
                    "stop_reason": "complete",
                    "rows": [{"keyword": "rack", "search_frequency_rank": 1, "products": []}],
                },
                "raw_rows": [{"keyword": "rack", "raw": True}],
            },
        )

    report = RemoteAbaClient(_client(handler)).fetch("rack", "2026-10-24")
    assert report.query == "rack"
    assert report.raw_rows == [{"keyword": "rack", "raw": True}]


def test_sellersprite_downloader_returns_bytes(tmp_path: Path) -> None:
    data = b"XLSXDATA"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "result": {"filename": "kw.xlsx", "count": 200},
                "files": {"kw.xlsx": _b64(data)},
            },
        )

    request = BrowserCollectionInput.model_validate({"direction": "forward", "query": "rack"})
    downloader = RemoteSellerspriteDownloader(_client(handler))
    filename, raw, count = downloader.download(request, tmp_path / "unused.xlsx")
    assert filename == "kw.xlsx"
    assert raw == data
    assert count == 200


def test_poe_downloader_hands_bytes_to_store(tmp_path: Path) -> None:
    csv = b"ASIN,Title\r\nB0TEST1234,Widget\r\n"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "result": {
                    "filename": "products.csv",
                    "source_url": (
                        "https://sellercentral.amazon.com/opportunity-explorer/"
                        "explore/niche/example/product"
                    ),
                },
                "files": {"products.csv": _b64(csv)},
            },
        )

    class FakeStore:
        received = b""

        def archive(
            self,
            source: Path,
            run_id: object,
            keyword: str,
            filename: str,
            source_url: str,
        ) -> PoeArchive:
            FakeStore.received = source.read_bytes()
            return PoeArchive(uuid4(), source, "text", "sha", [], filename, source_url)

    store = cast(PoeDownloadStore, FakeStore())
    archive = RemotePoeDownloader(_client(handler), store).download(
        uuid4(), "rack", "/opportunity-explorer/explore/niche/example"
    )
    assert archive.original_filename == "products.csv"
    assert FakeStore.received == csv


def test_remote_poe_intents_and_download_preserve_selected_reference_and_operation() -> None:
    requests: list[httpx.Request] = []
    csv = b"ASIN,Average Selling Price (Past 30 Days) (USD)\nB0TEST1234,20\n"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/intents"):
            return httpx.Response(
                200,
                json={
                    "result": [
                        {
                            "ref": "/opportunity-explorer/explore/niche/n-1",
                            "name": "adjustable dumbbell stand",
                            "top_keywords": ["powerblock stand"],
                            "metrics": {},
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "result": {
                    "filename": "筛选竞对ASIN-rack.csv",
                    "source_url": "https://sellercentral.amazon.com/opportunity-explorer/explore/niche/n-1/product",
                },
                "files": {"筛选竞对ASIN-rack.csv": _b64(csv)},
            },
        )

    class Store:
        def archive(
            self,
            source: Path,
            run_id: UUID,
            keyword: str,
            filename: str,
            source_url: str,
        ) -> PoeArchive:
            return PoeArchive(uuid4(), source, source.read_text(), "sha", [], filename, source_url)

    downloader = RemotePoeDownloader(_client(handler), cast(PoeDownloadStore, Store()))
    candidates = downloader.intents("rack")
    archive = downloader.download(uuid4(), "rack", candidates[0].ref)
    assert candidates[0].name == "adjustable dumbbell stand"
    assert archive.original_filename == "筛选竞对ASIN-rack.csv"
    assert json.loads(requests[1].content)["intent_ref"] == candidates[0].ref
