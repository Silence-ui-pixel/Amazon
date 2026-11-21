"""Amazon 前台页面：只复用或自动启动紫鸟店铺，不使用匿名浏览器兜底。"""

import json
from collections.abc import Generator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote
from urllib.request import Request, urlopen

from app.adapters.ziniao import ensure_port
from app.domain.errors import DomainError

if TYPE_CHECKING:
    from playwright.sync_api import Browser, BrowserContext, Page


def _reusable_context(browser: "Browser") -> "BrowserContext":
    """优先选已经有 Amazon 标签页的会话；无法唯一确定时不猜。"""
    contexts = browser.contexts
    preferred = [
        context
        for context in contexts
        if any("amazon." in (page.url or "") for page in context.pages)
    ]
    if len(preferred) == 1:
        return preferred[0]
    if len(contexts) == 1:
        return contexts[0]
    raise DomainError(
        503, "ZINIAO_BROWSER_AMBIGUOUS", "紫鸟中存在多个店铺会话，请只保留目标店铺后重试"
    )


def open_connected_tab(search_page: "Page", url: str) -> "Page":
    """在当前紫鸟连接里按地址打开标签，避免 context.new_page() 先停在空白页。"""
    from playwright.sync_api import Error as BrowserError

    browser = search_page.context.browser
    if browser is None:
        raise DomainError(503, "ZINIAO_CDP_UNAVAILABLE", "当前紫鸟连接不可用")
    created = browser.new_browser_cdp_session().send("Target.createTarget", {"url": url})
    target_id = str(created.get("targetId", ""))
    page = None
    for _ in range(20):
        page = next(
            (
                item
                for item in search_page.context.pages
                if item != search_page and url in (item.url or "") and item.url != "about:blank"
            ),
            None,
        )
        if page is not None:
            return page
        search_page.wait_for_timeout(200)
    if target_id:
        with suppress(BrowserError):
            browser.new_browser_cdp_session().send("Target.closeTarget", {"targetId": target_id})
    raise DomainError(503, "AMAZON_PAGE_UNAVAILABLE", "紫鸟页面没有打开")


def _open_target(port: int, url: str) -> str:
    """紫鸟实测：PUT /json/new?商品地址会直接打开商品页，不会先停在 about:blank。"""
    request = Request(f"http://127.0.0.1:{port}/json/new?{quote(url, safe=':/?&=')}", method="PUT")
    with urlopen(request, timeout=8) as response:
        payload = json.loads(response.read().decode())
    target_id = payload.get("id")
    if not isinstance(target_id, str) or not target_id:
        raise DomainError(503, "ZINIAO_TAB_OPEN_FAILED", "紫鸟没有返回可读取的商品标签")
    return target_id


def _close_target(port: int, target_id: str) -> None:
    with suppress(OSError):
        with urlopen(f"http://127.0.0.1:{port}/json/close/{target_id}", timeout=5):
            pass


@contextmanager
def amazon_product_tab(search_page: "Page", url: str) -> Generator["Page"]:
    """用当前紫鸟连接直接打开商品地址。

    紫鸟实测：新建标签会先停在 about:blank；再连一次浏览器会超时。
    只有当前连接的 Target.createTarget 能直接落到商品页。
    """
    from playwright.sync_api import Error as BrowserError

    browser = search_page.context.browser
    if browser is None:
        raise DomainError(503, "ZINIAO_CDP_UNAVAILABLE", "当前紫鸟连接不可用")
    page = open_connected_tab(search_page, url)
    try:
        yield page
    finally:
        with suppress(BrowserError):
            page.close()


@contextmanager
def amazon_product_page(directory: Path, url: str) -> Generator["Page"]:
    """为单个商品打开紫鸟标签，读完只关闭这个标签。"""
    from playwright.sync_api import Error as BrowserError
    from playwright.sync_api import sync_playwright

    port = ensure_port(directory, "KEYWORD_AMAZON_CDP_PORT", shared=directory.parent / "aba")
    target_id = _open_target(port, url)
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.connect_over_cdp(
                f"http://127.0.0.1:{port}", timeout=10000
            )
        except BrowserError:
            _close_target(port, target_id)
            raise DomainError(
                503, "ZINIAO_CDP_UNAVAILABLE", "紫鸟浏览器连接失败，请检查店铺是否已打开"
            ) from None
        context = _reusable_context(browser)
        page = next(
            (
                item
                for item in context.pages
                if getattr(item, "_impl_obj", None) is not None
                and getattr(item._impl_obj, "_guid", "").endswith(target_id)
            ),
            None,
        )
        if page is None:
            page = next((item for item in context.pages if url in (item.url or "")), None)
        if page is None:
            _close_target(port, target_id)
            raise DomainError(503, "AMAZON_PAGE_UNAVAILABLE", "紫鸟商品标签已打开但无法读取")
        try:
            yield page
        finally:
            with suppress(BrowserError):
                page.close()
            _close_target(port, target_id)


@contextmanager
def amazon_page(
    directory: Path, *, evidence_dir: Path | None = None
) -> Generator[tuple["Page", bool]]:
    """产出紫鸟页面；只关闭本次新建的标签页，不关闭用户浏览器。"""
    with amazon_pages(directory, 1) as pages:
        yield pages[0], True


@contextmanager
def amazon_pages(
    directory: Path, count: int
) -> Generator[list["Page"]]:
    """在同一紫鸟店铺上下文里开多个标签；每个标签只由创建它的线程使用。"""
    from playwright.sync_api import Error as BrowserError
    from playwright.sync_api import sync_playwright

    if count < 1:
        raise DomainError(500, "SEARCH_PAGE_COUNT_INVALID", "搜索标签数量必须至少为 1")
    port = ensure_port(directory, "KEYWORD_AMAZON_CDP_PORT", shared=directory.parent / "aba")
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.connect_over_cdp(
                f"http://127.0.0.1:{port}", timeout=10000
            )
        except BrowserError:
            raise DomainError(
                503, "ZINIAO_CDP_UNAVAILABLE", "紫鸟浏览器连接失败，请检查店铺是否已打开"
            ) from None
        context = _reusable_context(browser)
        pages = [context.new_page() for _ in range(count)]
        try:
            yield pages
        finally:
            for page in pages:
                with suppress(BrowserError):
                    page.close()
