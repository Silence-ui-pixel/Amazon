"""HTTP 版亚马逊搜索页/详情页采集：走 amazon-scrape-api，不再依赖本机浏览器。

只替换"取页"这一层：搜索页仍返回 SearchPage（含有序自然位），详情页仍返回
ProductSnapshot；上层排名、比对和落库逻辑不变。

重试与并发：
- 传输层（超时/连接失败/5xx/429）按指数退避重试，默认 3 次；
- 页面状态类的瞬时失败（CAPTCHA / NOT_READY / 页码不一致）同样重试；
- 参数错误、鉴权失败、商品不存在、ASIN 不一致属于确定性结果，不重试；
- 详情按 ASIN 并发拉取，默认 6 个线程；接口侧有全局并发保护，排队不会打爆代理。
"""

import os
import random
import time
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from typing import Any, cast

import httpx

from app.domain.asin_product import ProductSnapshot
from app.domain.errors import DomainError
from app.domain.verification import SearchCard, SearchPage
from app.services.search_policy import SearchFailure

BASE_URL = os.getenv("KEYWORD_AMAZON_SCRAPE_URL", "http://47.237.18.136:8097").rstrip("/")
TOKEN = os.getenv("KEYWORD_AMAZON_SCRAPE_TOKEN", "").strip()
TIMEOUT = float(os.getenv("KEYWORD_AMAZON_SCRAPE_TIMEOUT", "120") or "120")
ATTEMPTS = max(1, min(6, int(os.getenv("KEYWORD_AMAZON_SCRAPE_RETRIES", "3") or "3")))
BACKOFF = max(0.0, float(os.getenv("KEYWORD_AMAZON_SCRAPE_BACKOFF", "0.8") or "0.8"))
PRODUCT_WORKERS = max(1, min(12, int(os.getenv("KEYWORD_AMAZON_PRODUCT_WORKERS", "6") or "6")))
DEFAULT_ZIP = {"US": "10001"}

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}
# 页面没准备好、命中验证码、页码对不上：换一次出口通常就好了，属于可重试。
RETRYABLE_PAGE_STATUS = {"CAPTCHA", "NOT_READY", "SEARCH_PAGE_MISMATCH"}


def _backoff(attempt: int) -> None:
    """指数退避 + 少量抖动，避免多个线程同时重试把接口再打满。"""
    if attempt <= 0:
        return
    time.sleep(BACKOFF * (2 ** (attempt - 1)) + random.uniform(0.0, 0.3))


def _failure(code: str, message: str, *, retryable: bool) -> SearchFailure:
    error = SearchFailure(code, message)
    error.retryable = retryable  # type: ignore[attr-defined]
    return error


def _client_id() -> str:
    """调用方身份：同一台云服务器会替多个用户发请求，用采集端设备号区分配额。"""
    try:
        from app.adapters.remote_browser import current_browser_device_id

        raw = current_browser_device_id()
    except Exception:  # noqa: BLE001 - 标识缺失只影响配额区分，不影响抓取
        return ""
    if not raw:
        return ""
    # 抓取服务会把 UUID 形态的标识脱敏成 device-1（所有设备会塌缩成一个），
    # 换成带前缀的紧凑写法，才能让每台采集端各占一份配额。
    return "kwlib-" + raw.replace("-", "")[:16]


def _request(path: str, params: dict[str, object]) -> dict[str, Any]:
    """发一次请求；是否重试由调用方按 retryable 决定。"""
    query = {key: value for key, value in params.items() if value not in (None, "")}
    headers = {"X-Api-Token": TOKEN} if TOKEN else {}
    # 不带这个头时服务端按出口 IP 认调用方：所有用户共用一份 4 槽位配额。
    client_id = _client_id()
    if client_id:
        headers["X-Client-Id"] = client_id
    try:
        with httpx.Client(timeout=TIMEOUT, trust_env=False) as client:
            response = client.get(BASE_URL + path, params=query, headers=headers)
    except httpx.HTTPError as error:
        raise _failure(
            "SEARCH_NETWORK_ERROR", f"采集接口不可达：{type(error).__name__}", retryable=True
        ) from None
    if response.status_code == 401:
        raise _failure("SEARCH_ACCESS_CHECK", "采集接口鉴权失败，请检查 AMAZON_SCRAPE_TOKEN", retryable=False)
    if response.status_code in (400, 422):
        raise _failure(
            "AMAZON_SCRAPE_PARAMETER",
            f"采集接口参数错误（HTTP {response.status_code}）：请检查关键词长度与 ASIN 格式",
            retryable=False,
        )
    if response.status_code in RETRY_STATUS:
        raise _failure(
            "SEARCH_ACCESS_FAILED", f"采集接口返回 HTTP {response.status_code}", retryable=True
        )
    if response.status_code >= 400:
        raise _failure(
            "SEARCH_ACCESS_FAILED", f"采集接口返回 HTTP {response.status_code}", retryable=False
        )
    try:
        payload = response.json()
    except ValueError:
        raise _failure("SEARCH_NETWORK_ERROR", "采集接口返回的不是 JSON", retryable=True) from None
    if not isinstance(payload, dict):
        raise _failure("SEARCH_NETWORK_ERROR", "采集接口返回结构异常", retryable=True)
    return cast(dict[str, Any], payload)


def _evidence(payload: dict[str, Any]) -> str:
    evidence = payload.get("evidence")
    if isinstance(evidence, dict):
        return str(evidence.get("html_url") or "")
    return ""


def _search_page_once(keyword: str, page_number: int, site: str, zip_code: str, refresh: bool) -> SearchPage:
    payload = _request(
        "/api/amazon/search",
        {
            "keyword": keyword,
            "page": page_number,
            "site": site,
            "zip": zip_code,
            "refresh": 1 if refresh else 0,
        },
    )
    status = str(payload.get("status") or "")
    region = payload.get("region") if isinstance(payload.get("region"), dict) else {}
    region_text = str(cast(dict[str, Any], region).get("text") or "")
    url = str(payload.get("url") or "")

    if status == "NO_RESULTS":
        return SearchPage(region=region_text, cards=[], next_page=False, url=url)
    if status == "SEARCH_PAGE_MISMATCH":
        raise _failure("SEARCH_PAGE_MISMATCH", "采集接口返回的页码与请求不一致，本页不记排名", retryable=True)
    if status == "REGION_UNVERIFIED":
        raise _failure("SEARCH_DELIVERY_UNCONFIRMED", "配送地区未确认，不记录美国排名", retryable=False)
    if status == "NOT_READY":
        raise _failure("SEARCH_PAGE_UNVERIFIED", "搜索结果页未加载完整，不能当成没找到", retryable=True)
    if status == "CAPTCHA":
        raise _failure("SEARCH_ACCESS_CHECK", "Amazon 要求人工确认访问", retryable=True)
    if status != "OK":
        raise _failure("SEARCH_NETWORK_ERROR", f"采集接口失败：{status or '未知状态'}", retryable=True)

    cards: list[SearchCard] = []
    for row in payload.get("natural_cards") or []:
        if not isinstance(row, dict):
            continue
        asin = str(row.get("asin") or "")
        position = row.get("position")
        if len(asin) != 10 or not isinstance(position, int) or position < 1:
            continue
        cards.append(
            SearchCard(asin=asin, image_url=str(row.get("image_url") or ""), position=position)
        )
    cards.sort(key=lambda card: card.position)
    return SearchPage(
        region=region_text,
        cards=cards,
        next_page=bool(payload.get("next_page")),
        url=url,
        diagnostic=_evidence(payload),
    )


def fetch_search_page(
    keyword: str,
    page_number: int,
    *,
    site: str = "US",
    zip_code: str | None = None,
    refresh: bool = False,
) -> SearchPage:
    """取一页搜索结果；瞬时失败自动重试，确定性结果直接返回或报错。"""
    keyword = keyword.strip()
    if len(keyword) < 2:
        raise DomainError(422, "AMAZON_SCRAPE_PARAMETER", "搜索关键词至少 2 个字符")
    resolved_zip = zip_code if zip_code is not None else DEFAULT_ZIP.get(site, "")
    last: SearchFailure | None = None
    for attempt in range(ATTEMPTS):
        try:
            return _search_page_once(keyword, page_number, site, resolved_zip, refresh)
        except SearchFailure as error:
            if not getattr(error, "retryable", False) or attempt + 1 >= ATTEMPTS:
                raise
            last = error
            _backoff(attempt + 1)
    assert last is not None
    raise last


def fetch_search_pages(
    keyword: str,
    pages: list[int],
    *,
    site: str = "US",
    zip_code: str | None = None,
    refresh: bool = False,
    workers: int | None = None,
) -> dict[int, SearchPage | Exception]:
    """并发取同一个词的多个页码（各页互相独立，不需要按序）。"""
    wanted = [page for page in dict.fromkeys(pages) if page >= 1]
    if not wanted:
        return {}
    resolved = workers if workers is not None else min(4, len(wanted))
    results: dict[int, SearchPage | Exception] = {}
    with ThreadPoolExecutor(max_workers=max(1, resolved)) as pool:
        futures = {
            pool.submit(fetch_search_page, keyword, page, site=site, zip_code=zip_code, refresh=refresh): page
            for page in wanted
        }
        for future, page in futures.items():
            try:
                results[page] = future.result()
            except Exception as error:  # noqa: BLE001 - 单页失败不影响其他页
                results[page] = error
    return results


def _product_once(asin: str, site: str, zip_code: str, refresh: bool) -> ProductSnapshot:
    payload = _request(
        f"/api/amazon/product/{asin}",
        {"site": site, "zip": zip_code, "refresh": 1 if refresh else 0},
    )
    status = str(payload.get("status") or "")
    if status == "ASIN_IDENTITY_UNCONFIRMED":
        raise _failure(
            "ASIN_IDENTITY_UNCONFIRMED",
            "商品页实际 ASIN 与输入不一致（可能是父体或已下架变体），未使用页面资料",
            retryable=False,
        )
    if status == "NOT_FOUND":
        raise _failure("ASIN_DETAILS_UNAVAILABLE", "商品页不存在或已下架", retryable=False)
    if status == "CAPTCHA":
        raise _failure("SEARCH_ACCESS_CHECK", "商品页需要人工确认访问", retryable=True)
    if status != "OK":
        raise _failure("SEARCH_NETWORK_ERROR", f"采集接口失败：{status or '未知状态'}", retryable=True)

    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    record = cast(dict[str, Any], data)
    found = str(record.get("asin") or "")
    # 与浏览器版一致：页面真实 ASIN 必须等于请求值，否则一律不采用页面资料。
    if found != asin:
        raise _failure("ASIN_IDENTITY_UNCONFIRMED", "详情返回的 ASIN 与请求不一致", retryable=False)
    title = str(record.get("title") or "").strip()
    if not title:
        raise _failure(
            "ASIN_DETAILS_UNAVAILABLE", "商品标题未能读取，可能是页面不可用或结构发生变化", retryable=False
        )

    raw_price = record.get("price")
    price: Decimal | None = None
    if raw_price not in (None, ""):
        try:
            parsed = Decimal(str(raw_price))
        except (InvalidOperation, ValueError):
            parsed = Decimal("0")
        if parsed > 0:
            price = parsed
    currency = str(record.get("currency") or "") or None
    if currency is not None and len(currency) != 3:
        currency = None
    return ProductSnapshot(
        asin=asin,
        title=title[:2000],
        features=[str(item)[:2000] for item in (record.get("features") or [])][:20],
        description=str(record.get("description") or "")[:10000],
        image_url=str(record.get("image_url") or ""),
        source_url=str(payload.get("url") or ""),
        price=price,
        currency=currency,
        price_reliable=bool(record.get("price_reliable", True)),
    )


def fetch_product(
    asin: str, *, site: str = "US", zip_code: str | None = None, refresh: bool = False
) -> ProductSnapshot:
    """取单个商品详情；身份不符或验证码一律不冒充商品不存在。"""
    resolved_zip = zip_code if zip_code is not None else DEFAULT_ZIP.get(site, "")
    last: SearchFailure | None = None
    for attempt in range(ATTEMPTS):
        try:
            return _product_once(asin, site, resolved_zip, refresh)
        except SearchFailure as error:
            if not getattr(error, "retryable", False) or attempt + 1 >= ATTEMPTS:
                raise
            last = error
            _backoff(attempt + 1)
    assert last is not None
    raise last


class ApiSearchSession:
    """与 BrowserSession 相同的会话协议，把取页换成 HTTP 接口。"""

    def __init__(self, marketplace: str = "US", zip_code: str | None = None) -> None:
        self.marketplace = marketplace
        self.zip_code = zip_code if zip_code is not None else DEFAULT_ZIP.get(marketplace)
        # 接口按 (site, zip) 自己锁定配送地址，调用方不需要再传"已设置过邮编"。
        self.delivery_ready = True

    def search(self, keyword: str, page_number: int, evidence_name: str) -> SearchPage:
        return fetch_search_page(
            keyword, page_number, site=self.marketplace, zip_code=self.zip_code
        )

    def search_pages(self, keyword: str, pages: list[int], evidence_name: str = "") -> dict[int, SearchPage | Exception]:
        """同一个词多页并发取；页码之间没有依赖。"""
        return fetch_search_pages(keyword, pages, site=self.marketplace, zip_code=self.zip_code)

    def product(self, asin: str) -> ProductSnapshot:
        return fetch_product(asin, site=self.marketplace)

    def products(self, asins: list[str]) -> list[tuple[str, ProductSnapshot | None, Exception | None]]:
        """并发拉详情；线程数可配，默认 4。"""
        if not asins:
            return []
        ordered = list(dict.fromkeys(asins))
        results: dict[str, tuple[str, ProductSnapshot | None, Exception | None]] = {}
        with ThreadPoolExecutor(max_workers=min(PRODUCT_WORKERS, len(ordered))) as pool:
            futures = {pool.submit(self.product, asin): asin for asin in ordered}
            for future in futures:
                asin = futures[future]
                try:
                    results[asin] = (asin, future.result(), None)
                except Exception as error:  # noqa: BLE001 - 与浏览器会话保持同样的逐条失败语义
                    results[asin] = (asin, None, error)
        return [results[asin] for asin in asins if asin in results]


@contextmanager
def open_api_session(
    marketplace: str = "US", zip_code: str | None = None
) -> Generator[ApiSearchSession]:
    yield ApiSearchSession(marketplace, zip_code)
