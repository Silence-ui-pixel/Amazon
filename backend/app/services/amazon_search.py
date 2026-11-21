"""Amazon 搜索页采集；只读取自然商品卡片，不绕过访问验证。"""

import json
import os
import re
import time
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import AbstractContextManager, contextmanager, suppress
from email.utils import parsedate_to_datetime
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlencode

from pydantic import BaseModel, ValidationError

from app.adapters.ziniao import data_directory
from app.domain.errors import DomainError
from app.domain.verification import SearchCard, SearchPage
from app.services.amazon_page import amazon_page
from app.services.browser_runtime import launch_browser
from app.services.search_policy import SearchFailure
from app.services.search_policy import retry_search as retry_search

if TYPE_CHECKING:
    from playwright.sync_api import Page

    from app.domain.asin_product import ProductSnapshot


HOSTS = {
    "US": "www.amazon.com",
    "CA": "www.amazon.ca",
    "UK": "www.amazon.co.uk",
    "DE": "www.amazon.de",
    "FR": "www.amazon.fr",
    "IT": "www.amazon.it",
    "ES": "www.amazon.es",
    "JP": "www.amazon.co.jp",
}


class RawCard(BaseModel):
    asin: str
    image_url: str = ""
    sponsored: bool


class Snapshot(BaseModel):
    region: str
    ready: bool
    next_page: bool
    pagination_known: bool
    cards: list[RawCard]
    page_number: int | None = None
    no_results: bool = False


def parse_snapshot(raw: object, expected_page: int | None = None) -> SearchPage:
    try:
        data = Snapshot.model_validate(raw)
    except ValidationError:
        raise DomainError(
            502, "SEARCH_PARSE_FAILED", "搜索页面结构无法确认，不能按未找到处理"
        ) from None
    if expected_page is not None and data.page_number != expected_page and not (
        data.no_results and expected_page == 1 and data.page_number is None
    ):
        raise DomainError(502, "SEARCH_PAGE_MISMATCH", "无法确认当前搜索页码，不记录排名")
    if (
        not data.ready
        or not data.region.strip()
        or (not data.pagination_known and not data.no_results)
        or (not data.cards and not data.no_results)
    ):
        raise DomainError(502, "SEARCH_PAGE_UNVERIFIED", "搜索结果、配送地区或分页未能确认")
    cards: list[SearchCard] = []
    for card in data.cards:
        if not re.fullmatch(r"[A-Z0-9]{10}", card.asin):
            raise DomainError(502, "SEARCH_PARSE_FAILED", "商品卡片缺少可靠 ASIN")
        if not card.sponsored:
            cards.append(
                SearchCard(asin=card.asin, image_url=card.image_url, position=len(cards) + 1)
            )
    return SearchPage(region=" ".join(data.region.split()), cards=cards, next_page=data.next_page)


class SearchSession(Protocol):
    def search(self, keyword: str, page_number: int, evidence_name: str) -> SearchPage: ...

    def products(self, asins: list[str]) -> list[tuple[str, "ProductSnapshot | None", Exception | None]]: ...


class SearchProvider(Protocol):
    def open(
        self, marketplace: str, evidence_dir: Path
    ) -> AbstractContextManager[SearchSession]: ...


# 定位 Sponsored 的文字与结构标记；广告卡片在自然位置计数之前移除。
EXTRACT = r"""() => {
 const slot=document.querySelector('.s-main-slot');
 const cards=[...(slot?.querySelectorAll('[data-component-type="s-search-result"][data-asin]')||[])];
 const adText=/Sponsored|Gesponsert|Sponsorisé|Sponsorizzato|Patrocinado|スポンサー/i;
 const next=document.querySelector('.s-pagination-next');
 return {
  ready:!!slot && !document.querySelector('#captchacharacters,form[action*="validateCaptcha"]'),
  no_results:!!document.querySelector('.s-no-results-container') || /no results for|did not match any products/i.test(slot?.innerText || ''),
  region:document.querySelector('#glow-ingress-line2')?.innerText || '',
  pagination_known:!!next,
  page_number:Number(document.querySelector('.s-pagination-selected')?.textContent)||null,
  next_page:!!next && next.tagName==='A' && !next.classList.contains('s-pagination-disabled'),
  cards:cards.map(c=>({
   asin:c.getAttribute('data-asin')||'', image_url:c.querySelector('img.s-image')?.src||'',
   sponsored:c.classList.contains('AdHolder') || !!c.querySelector('.puis-sponsored-label-text,[data-component-type="sp-sponsored-result"],.s-sponsored-label-info-icon') ||
    [...c.querySelectorAll('a,span')].some(e=>adText.test(e.getAttribute('aria-label')||'') || (e.children.length===0 && adText.test(e.textContent.trim()) && e.textContent.trim().length<40))
  }))
 };
}"""


def require_us_delivery(region: str, zipcode: str) -> None:
    """仅记录已确认邮编的排名，不能用美国域名或模糊国家文字代替确认。"""
    if not re.search(rf"(?<!\d){re.escape(zipcode)}(?!\d)", region) or re.search(
        r"china|中国", region, re.IGNORECASE
    ):
        raise DomainError(
            502, "SEARCH_DELIVERY_UNCONFIRMED", "配送地区未确认或已变化，未记录美国排名"
        )


def configure_us_delivery(page: "Page", zipcode: str) -> None:
    """通过 Amazon 正常地址弹窗设置邮编；只影响此浏览器上下文。

    弹窗依赖页面脚本渲染，冷启动或导航尚未完成时打不开；重载后重试几次，
    仍然失败才报错。其他地方一律不使用替代地区。
    """
    from playwright.sync_api import Error as BrowserError
    from playwright.sync_api import TimeoutError as BrowserTimeout

    def confirmed() -> bool:
        region = page.locator("#glow-ingress-line2").inner_text(timeout=5000)
        return zipcode in region and "china" not in region.casefold()

    def attempt() -> bool:
        try:
            with suppress(BrowserTimeout):
                page.wait_for_load_state("load", timeout=15000)
            with suppress(BrowserError, DomainError):
                if confirmed():
                    return True
            with suppress(BrowserError):
                # 上一次尝试可能留下半开弹窗，先关掉再点开。
                page.keyboard.press("Escape")
            page.locator("#nav-global-location-popover-link").click(timeout=15000)
            field = page.locator("#GLUXZipUpdateInput")
            field.wait_for(state="visible", timeout=20000)
            field.fill(zipcode, timeout=20000)
            page.locator("#GLUXZipUpdate").click(timeout=10000)
            # 地址确认由页面处理完成，不能在请求尚未提交时立即导航。
            page.wait_for_function(
                "zip => document.querySelector('#glow-ingress-line2')?.innerText.includes(zip) || "
                "[...document.querySelectorAll('.a-popover-wrapper')].some(e => "
                "e.innerText.includes(zip) && /United States/.test(e.innerText))",
                arg=zipcode,
                timeout=15000,
            )
            # Amazon 有时在隐藏的旧弹窗中保留同名 ID，只点击当前可见按钮。
            confirm = page.locator(
                "#GLUXConfirmClose:visible, button[name='glowDoneButton']:visible"
            )
            if confirm.count():
                confirm.first.click(timeout=10000)
            return True
        except BrowserError:
            return False

    for index in range(3):
        if attempt():
            return
        with suppress(BrowserError):
            page.reload(wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(1500 * (index + 1))
    raise DomainError(
        502, "SEARCH_DELIVERY_SETUP_FAILED", "无法设置美国配送邮编，未使用其他地区替代"
    )


class AmazonSearch:
    """任务内共用上下文；优先复用紫鸟店铺，退回独立浏览器时保存 Cookie。"""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or data_directory()

    @contextmanager
    def open(self, marketplace: str, evidence_dir: Path) -> Generator[SearchSession]:
        from playwright.sync_api import Error as BrowserError

        evidence_dir.mkdir(parents=True, exist_ok=True)
        zipcode = os.getenv("KEYWORD_US_DELIVERY_ZIP", "10001").strip()
        if marketplace == "US" and not re.fullmatch(r"[0-9]{5}", zipcode):
            raise DomainError(503, "SEARCH_DELIVERY_CONFIG_INVALID", "美国配送邮编必须为5位数字")
        try:
            with amazon_page(self.directory, evidence_dir=evidence_dir) as (page, _ziniao):
                from app.services.asin_product import AmazonProductProvider

                yield BrowserSession(
                    page, marketplace, zipcode, evidence_dir, AmazonProductProvider(self.directory)
                )
        except DomainError:
            raise
        except (BrowserError, OSError):
            raise DomainError(
                503, "SEARCH_BROWSER_UNAVAILABLE", "搜索浏览器不可用，请检查浏览器安装与会话文件"
            ) from None

    def review(
        self,
        marketplace: str,
        evidence_dir: Path,
        keyword: str,
        stop: Event,
        ready: Callable[[], None],
    ) -> None:
        """只打开正常浏览器供人工操作，不自动点击或解答访问检查。"""
        from playwright.sync_api import Error as BrowserError
        from playwright.sync_api import sync_playwright

        state = evidence_dir / "browser-state.json"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as p:
            browser = launch_browser(p, headless=False)
            try:
                context = browser.new_context(
                    locale="en-US", storage_state=str(state) if state.is_file() else None
                )
                page = context.new_page()
                with suppress(BrowserError):
                    page.goto(
                        f"https://{HOSTS[marketplace]}/s?" + urlencode({"k": keyword}),
                        wait_until="domcontentloaded",
                        timeout=45000,
                    )
                ready()
                deadline = time.monotonic() + 600
                while not stop.is_set() and time.monotonic() < deadline and not page.is_closed():
                    page.wait_for_timeout(500)
                with suppress(BrowserError):
                    context.storage_state(path=str(state))
            finally:
                browser.close()


class BrowserSession:
    def __init__(
        self,
        page: "Page",
        marketplace: str,
        zipcode: str,
        evidence_dir: Path,
        product_provider: object | None = None,
    ) -> None:
        self.page = page
        self.marketplace = marketplace
        self.zipcode = zipcode
        self.evidence_dir = evidence_dir
        self.product_provider = product_provider
        self.delivery_ready = False
        self.last_navigation = 0.0
        self.stage = "navigation"
        self.stage_started = time.monotonic()
        self.timings: dict[str, float] = {}

    def set_stage(self, stage: str) -> None:
        now = time.monotonic()
        self.timings[self.stage] = self.timings.get(self.stage, 0) + now - self.stage_started
        self.stage_started = now
        self.stage = stage

    def navigate(self, url: str) -> None:
        # 每次导航至少间隔三秒，包括设置配送地区后的刷新和重试。
        time.sleep(max(0, 3 - (time.monotonic() - self.last_navigation)))
        self.last_navigation = time.monotonic()
        response = self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
        if response is None:
            raise SearchFailure("SEARCH_NETWORK_ERROR", "搜索请求未返回响应")
        self.check_access()
        if response.status in (429, 503):
            raw = response.headers.get("retry-after", "0")
            try:
                delay = float(raw)
            except ValueError:
                try:
                    delay = max(0, parsedate_to_datetime(raw).timestamp() - time.time())
                except (ValueError, TypeError, OverflowError):
                    delay = 0
            raise SearchFailure(
                "SEARCH_RATE_LIMITED" if response.status == 429 else "SEARCH_SERVICE_UNAVAILABLE",
                f"搜索服务暂时不可用（HTTP {response.status}）",
                retry_after=delay,
            )
        if response.status != 200:
            raise SearchFailure("SEARCH_ACCESS_FAILED", f"搜索页返回 HTTP {response.status}")

    def check_access(self) -> None:
        blocked = self.page.evaluate(
            r"""() => /\/checkpoint\/|validateCaptcha/.test(location.href) || !!document.querySelector('#captchacharacters,form[action*=validateCaptcha]') || (!document.querySelector('.s-main-slot,#productTitle') && /continue shopping|robot check|enter the characters you see/i.test(document.body?.innerText || ''))"""
        )
        if blocked:
            raise SearchFailure(
                "SEARCH_ACCESS_CHECK", "Amazon 要求人工确认访问，请打开检查窗口处理"
            )

    def search(self, keyword: str, page_number: int, evidence_name: str) -> SearchPage:
        return retry_search(
            lambda attempt: self._attempt(keyword, page_number, evidence_name, attempt)
        )

    def product(self, asin: str) -> "ProductSnapshot":
        """读取单个详情，沿用当前搜索上下文。

        紫鸟连接是线程绑定的：必须复用本页面的连接在同一线程里开标签，
        换线程使用会直接报 "Cannot switch to a different thread"。
        """
        from app.services.amazon_page import amazon_product_tab
        from app.services.asin_product import read_product_page

        url = f"https://{HOSTS[self.marketplace]}/dp/{asin}"
        with amazon_product_tab(self.page, url) as page:
            return read_product_page(
                page, self.marketplace, asin, delivery_ready=self.delivery_ready
            )

    def products(self, asins: list[str]) -> list[tuple[str, "ProductSnapshot | None", Exception | None]]:
        """用独立 Playwright 连接并发读取详情，不在线程间共享 Page/Browser。"""
        from app.services.product_term_collection import PRODUCT_CONCURRENCY

        results: dict[str, tuple[str, "ProductSnapshot | None", Exception | None]] = {}
        with ThreadPoolExecutor(max_workers=min(PRODUCT_CONCURRENCY, len(asins))) as pool:
            futures = {
                pool.submit(
                    self.product_provider.fetch,
                    self.marketplace,
                    asin,
                    delivery_ready=self.delivery_ready,
                ): asin
                for asin in asins
            }
            for future in as_completed(futures):
                asin = futures[future]
                try:
                    results[asin] = (asin, future.result(), None)
                except Exception as error:
                    results[asin] = (asin, None, error)
        return [results[asin] for asin in asins]

    def _attempt(self, keyword: str, page_number: int, name: str, attempt: int) -> SearchPage:
        from playwright.sync_api import Error as BrowserError
        from playwright.sync_api import TimeoutError as BrowserTimeout

        started = time.monotonic()
        self.stage_started = started
        self.timings = {}
        self.stage = "navigation"
        url = f"https://{HOSTS[self.marketplace]}/s?" + urlencode(
            {"k": keyword, "page": page_number}
        )
        try:
            self.navigate(url)
            if self.marketplace == "US" and not self.delivery_ready:
                self.set_stage("delivery")
                region = self.page.locator("#glow-ingress-line2").inner_text(timeout=5000)
                try:
                    require_us_delivery(region, self.zipcode)
                except DomainError:
                    configure_us_delivery(self.page, self.zipcode)
                    self.navigate(url)
                self.delivery_ready = True
            self.set_stage("results")
            self.page.locator(".s-main-slot").wait_for(timeout=12000)
            self.check_access()
            result = parse_snapshot(self.page.evaluate(EXTRACT), expected_page=page_number)
            if self.marketplace == "US":
                require_us_delivery(result.region, self.zipcode)
            result.url = url
            result.screenshot = name + ".png"
            self.set_stage("screenshot")
            self.page.screenshot(
                path=str(self.evidence_dir / result.screenshot), full_page=True, timeout=15000
            )
            self.set_stage("finished")
            result.diagnostic = f"{name}-attempt-{attempt}.json"
            diagnostic = {
                "code": "OK",
                "attempt": attempt,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "stage_seconds": self.timings,
                "keyword": keyword,
                "page": page_number,
                "url": url,
            }
            (self.evidence_dir / result.diagnostic).write_text(
                json.dumps(diagnostic, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return result
        except (DomainError, BrowserError) as error:
            if isinstance(error, DomainError):
                failure = (
                    error
                    if isinstance(error, SearchFailure)
                    else SearchFailure(error.code, error.message)
                )
            else:
                # 主体等待超时后再检查一次，避免把验证页误报成普通网络超时。
                try:
                    self.check_access()
                except SearchFailure as checked:
                    failure = checked
                except BrowserError:
                    failure = SearchFailure("SEARCH_NETWORK_ERROR", "搜索连接中断，页面不可读取")
                else:
                    failure = SearchFailure(
                        "SEARCH_NETWORK_TIMEOUT"
                        if isinstance(error, BrowserTimeout)
                        else "SEARCH_NETWORK_ERROR",
                        "搜索页面加载超时" if isinstance(error, BrowserTimeout) else "搜索连接中断",
                    )
            failure.stage = self.stage
            failure.screenshot = f"{name}-attempt-{attempt}.png"
            failure.diagnostic = f"{name}-attempt-{attempt}.json"
            try:
                self.page.screenshot(path=str(self.evidence_dir / failure.screenshot), timeout=5000)
            except BrowserError:
                failure.screenshot = ""
            # 不保存 Cookie、密钥或页面正文；会话状态独立存放且不由证据接口暴露。
            self.timings[self.stage] = (
                self.timings.get(self.stage, 0) + time.monotonic() - self.stage_started
            )
            diagnostic = {
                "stage_seconds": self.timings,
                "stage": self.stage,
                "code": failure.code,
                "attempt": attempt,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "keyword": keyword,
                "page": page_number,
                "url": url,
                "retry_after": failure.retry_after,
            }
            (self.evidence_dir / failure.diagnostic).write_text(
                json.dumps(diagnostic, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            raise failure from None
