"""Agent 运行态：同一紫鸟店铺多搜索通道 + 会话注册表 + 本地数据目录。"""

import os
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Protocol, TypeVar

from agent.version import AGENT_VERSION
from app.adapters.aba import AbaClient
from app.adapters.poe_downloads import PoeBrowserDownloader, PoeDownloadStore
from app.adapters.sellersprite_browser import SellerspriteBrowserDownloader
from app.domain.asin_product import ProductSnapshot
from app.domain.errors import DomainError
from app.domain.verification import SearchPage
from app.services.amazon_page import amazon_pages
from app.services.amazon_search import AmazonSearch, BrowserSession
from app.services.asin_product import AmazonProductProvider

T = TypeVar("T")

# 会话空闲超过此时长自动关闭：强杀服务器后，残留会话会一直占着唯一工作线程的 Playwright 事件循环。
SESSION_TTL = 600.0

class Session(Protocol):
    """搜索会话的完整能力：翻页搜索 + 详情读取。"""

    def search(self, keyword: str, page_number: int, evidence_name: str) -> SearchPage: ...

    def product(self, asin: str) -> ProductSnapshot: ...


def agent_data_dir() -> Path:
    """Agent 的会话/凭据目录；服务器数据目录不复用，避免本机联调互相覆盖。"""
    configured = os.getenv("KEYWORD_AGENT_DATA_DIR", "").strip()
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[1] / "agent" / "data"


class LaneHandle:
    """一个关键词搜索通道：独立标签页，只能由占用它的工作线程操作。"""

    def __init__(self, session: BrowserSession) -> None:
        self.session = session
        self.lock = Lock()


class SessionHandle:
    """持有已打开的搜索会话；每个搜索通道有独立标签，互不共享 Page。"""

    def __init__(
        self,
        marketplace: str,
        evidence_dir: Path,
        lanes: list[LaneHandle],
        closer: Callable[[], None],
    ) -> None:
        self.marketplace = marketplace
        self.evidence_dir = evidence_dir
        self.lanes = lanes
        self._closer = closer
        self.last_used = time.monotonic()
        self._lane_guard = Lock()

    @property
    def session(self) -> BrowserSession:
        """详情读取不占用搜索标签；搜索必须走 acquire_lane()。"""
        if not self.lanes:
            raise DomainError(503, "AGENT_SEARCH_UNAVAILABLE", "搜索会话没有可用标签")
        return self.lanes[0].session

    def close(self) -> None:
        self._closer()

    def acquire_lane(self, timeout: float = 180.0) -> LaneHandle:
        deadline = time.monotonic() + timeout
        while True:
            with self._lane_guard:
                free = next((lane for lane in self.lanes if lane.lock.acquire(blocking=False)), None)
            if free is not None:
                return free
            if time.monotonic() >= deadline:
                raise DomainError(409, "AGENT_SEARCH_BUSY", "搜索通道都在使用，请减少并发后重试")
            time.sleep(0.05)


class AgentRuntime:
    def __init__(self, data: Path | None = None) -> None:
        self.data = data or agent_data_dir()
        # 旧版本只保存商品采集的店铺；启动时同步给 ABA 和卖家精灵。
        with suppress(OSError):
            saved_store = (self.data / "amazon" / "store_id").read_text(encoding="utf-8").strip()
            if saved_store.isdecimal():
                self.select_store(saved_store)
        # Playwright 的同步对象是「线程绑定」的：页面在哪条线程创建，就只能在那条线程用。
        # 之前用 4 线程池，页面在 A 线程打开、搜索落到 B 线程，就会抛
        # "Playwright Sync API inside the asyncio loop"，表现为每个词根 0 页失败。
        # 这里固定为单线程，保证创建与使用永远同线程；并发交给会话内的 lanes 标签页。
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="browser-agent")
        self.lock = Lock()
        self.sessions: dict[str, SessionHandle] = {}
        # /v1/status 用的运行状态（供服务器前端展示「浏览器 Agent」状态）。
        self._active = 0
        self._operation = ""
        self._operation_id = ""
        self._operation_started: str | None = None
        self._last_error_code = ""
        self.search = AmazonSearch(self.data / "amazon")
        self.products = AmazonProductProvider(self.data / "amazon")
        self.aba = AbaClient.from_directory(self.data / "aba")
        # 卖家精灵：紫鸟浏览器优先（与 ABA 同源），download() 内会自动回落 9222；
        # KEYWORD_SELLERSPRITE_BROWSER_PORT 可显式指定端口跳过发现。
        _ss_port = os.environ.get("KEYWORD_SELLERSPRITE_BROWSER_PORT", "").strip()
        self.sellersprite = SellerspriteBrowserDownloader(
            int(_ss_port) if _ss_port.isdecimal() else None,
            directory=self.data / "sellersprite",
        )
        self.poe = PoeBrowserDownloader(
            self.data / "aba", PoeDownloadStore(self.data / "poe_downloads")
        )

    def select_store(self, store_id: str) -> str:
        from app.adapters.ziniao import select_store

        selected = select_store(self.data / "amazon", store_id)
        for name in ("aba", "sellersprite"):
            select_store(self.data / name, selected)
        return selected

    def run(self, fn: Callable[[], T]) -> T:
        def _wrapped() -> T:
            self._close_stale()
            try:
                return fn()
            except DomainError:
                raise
            except Exception as error:
                # 浏览器/Playwright 的瞬时错误（如 net::ERR_ABORTED）不能变成 500；
                # 统一转成可重试的错误码，由 ops 层封装进信封。
                import logging

                logging.getLogger(__name__).exception("browser operation failed")
                raise DomainError(
                    503,
                    "AGENT_BROWSER_ERROR",
                    f"浏览器操作失败（{type(error).__name__}: {str(error)[:240]}）",
                ) from None

        self._active += 1
        self._operation = getattr(fn, "__name__", "operation").lstrip("_") or "operation"
        self._operation_id = uuid.uuid4().hex[:12]
        self._operation_started = datetime.now(UTC).isoformat()
        try:
            return self.executor.submit(_wrapped).result()
        except DomainError as error:
            self._last_error_code = error.code
            raise
        except Exception:
            self._last_error_code = "AGENT_INTERNAL_ERROR"
            raise
        finally:
            self._active -= 1
            if self._active <= 0:
                self._active = 0
                self._operation = ""
                self._operation_id = ""
                self._operation_started = None

    def status(self) -> dict[str, object]:
        """服务器轮询的 Agent 状态（GET /v1/status）。"""
        state = "busy" if self._active > 0 else "online"
        return {
            "state": state,
            "operation": self._operation,
            "operation_id": self._operation_id,
            "started_at": self._operation_started,
            "last_error_code": self._last_error_code,
            "version": AGENT_VERSION,
        }

    def _close_stale(self) -> None:
        """在唯一工作线程上清理空闲会话；必须在 run 内调用，保证与 enter/exit 同线程。"""
        now = time.monotonic()
        with self.lock:
            stale = [
                sid for sid, handle in self.sessions.items() if now - handle.last_used > SESSION_TTL
            ]
        for sid in stale:
            with self.lock:
                handle = self.sessions.pop(sid, None)
            if handle is not None:
                handle.close()

    def open_session(self, marketplace: str, lanes: int = 1) -> str:
        from app.services.product_term_collection import SEARCH_CONCURRENCY

        count = max(1, min(SEARCH_CONCURRENCY, lanes))

        def _open() -> str:
            # 编排被中断时旧会话可能没走 DELETE，Playwright 同步实例会一直占着唯一
            # 工作线程的事件循环；先全部收掉，否则新会话会报
            # "Playwright Sync API inside the asyncio loop" 并整步失败。
            with self.lock:
                leftover = list(self.sessions.values())
                self.sessions.clear()
            for handle in leftover:
                with suppress(Exception):
                    handle.close()
            session_id = uuid.uuid4().hex
            evidence = self.data / "spool" / session_id
            evidence.mkdir(parents=True, exist_ok=True)
            zipcode = os.getenv("KEYWORD_US_DELIVERY_ZIP", "10001").strip()
            from app.services.amazon_page import open_connected_tab
            from app.services.amazon_search import HOSTS

            home = f"https://{HOSTS[marketplace]}/"
            opened = amazon_pages(self.data / "amazon", 1)
            seed = opened.__enter__()[0]
            pages = [open_connected_tab(seed, home) for _ in range(count)]
            with suppress(Exception):
                seed.close()
            handle = SessionHandle(
                marketplace,
                evidence,
                [
                    LaneHandle(
                        BrowserSession(page, marketplace, zipcode, evidence, self.products)
                    )
                    for page in pages
                ],
                lambda: opened.__exit__(None, None, None),
            )
            self.sessions[session_id] = handle
            return session_id

        return self.run(_open)

    def get(self, session_id: str) -> SessionHandle:
        with self.lock:
            handle = self.sessions.get(session_id)
        if handle is None:
            raise DomainError(404, "AGENT_SESSION_NOT_FOUND", "搜索会话不存在或已关闭")
        handle.last_used = time.monotonic()
        return handle

    def close_session(self, session_id: str) -> None:
        def _close() -> None:
            handle = self.sessions.pop(session_id, None)
            if handle is not None:
                handle.close()

        self.run(_close)

    def shutdown(self) -> None:
        for handle in list(self.sessions.values()):
            handle.close()
        self.sessions.clear()
        self.executor.shutdown(wait=False, cancel_futures=True)
