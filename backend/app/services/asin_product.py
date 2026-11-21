"""按站点读取 Amazon 商品详情；身份不明、验证码或访问限制不冒充商品不存在。"""

import asyncio
import logging
import os
import re
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast
from urllib.parse import urlsplit
from uuid import UUID

from app.adapters.ziniao import data_directory
from app.db.repository import Repository
from app.domain.asin_product import ProductSnapshot
from app.domain.errors import DomainError
from app.services.amazon_page import amazon_product_page
from app.services.amazon_search import HOSTS, configure_us_delivery, require_us_delivery
from app.services.search_policy import SearchFailure

if TYPE_CHECKING:
    from playwright.sync_api import Page


def parse_product_snapshot(data: Mapping[str, object], asin: str) -> ProductSnapshot:
    """只接收详情主体的 ASIN；不能将推荐区或父体商品当作请求商品。"""
    if data.get("blocked"):
        raise DomainError(502, "ASIN_ACCESS_BLOCKED", "商品页需要验证或受到访问限制，请稍后重试")
    found = data.get("asin")
    if not isinstance(found, str) or not found.strip():
        raise DomainError(
            502,
            "ASIN_DETAILS_UNAVAILABLE",
            "商品页未能读取到主体 ASIN，可能尚未加载完成或页面结构变化，请稍后重试",
        )
    if found != asin:
        # 常见原因：输入的是父体/已下架变体，亚马逊自动跳到同变体组的默认款。
        detail = f"页面实际商品为 {found}" if found else "页面未取到 ASIN"
        raise DomainError(
            502,
            "ASIN_IDENTITY_UNCONFIRMED",
            f"商品页 ASIN 与输入不一致（{detail}），未使用页面资料；请核对输入 ASIN 是否为可售子体",
        )
    title = data.get("title")
    if not isinstance(title, str) or not title.strip():
        raise DomainError(
            502, "ASIN_DETAILS_UNAVAILABLE", "商品标题未能读取，可能是页面不可用或结构发生变化"
        )
    features = data.get("features", [])
    raw_price = data.get("price_text")
    amount = None
    declared = data.get("currency")
    currency = (
        declared if isinstance(declared, str) and re.fullmatch(r"[A-Z]{3}", declared) else None
    )
    if isinstance(raw_price, str):
        match = re.fullmatch(
            r"(USD|CAD|GBP|EUR|JPY|[$£€¥])?\s*(\d+(?:,\d{3})*(?:\.\d{1,4})?)", raw_price.strip()
        )
        if match:
            parsed = Decimal(match[2].replace(",", ""))
            if 0 < parsed < Decimal("10000000000"):
                amount = parsed
            token = match[1]
            evidence = {"€": "EUR", "£": "GBP"}.get(token, token)
            if token in ("$", "¥"):
                allowed = (
                    {"USD", "CAD", "AUD", "NZD", "SGD", "HKD"} if token == "$" else {"JPY", "CNY"}
                )
                currency = currency if currency in allowed else None
            elif evidence:
                currency = evidence if currency is None or currency == evidence else None
    return ProductSnapshot(
        asin=asin,
        title=title.strip()[:2000],
        features=[str(value)[:2000] for value in cast(list[object], features)[:20]]
        if isinstance(features, list)
        else [],
        description=str(data.get("description", ""))[:10000],
        image_url=str(data.get("image_url", "")),
        price=amount,
        currency=currency,
        price_reliable=(
            amount is not None
            and currency is not None
            and data.get("offer_kind") not in {"subscription", "coupon", "range"}
        ),
    )


class ProductProvider(Protocol):
    def fetch(self, marketplace: str, asin: str) -> ProductSnapshot: ...


EXTRACT_PRODUCT = r"""() => ({
 ...(() => {
   const priceSelectors = [
     '#corePriceDisplay_desktop_feature_div .a-price:not(.a-text-price) .a-offscreen',
     '#corePrice_feature_div .a-price:not(.a-text-price) .a-offscreen',
     '#corePrice_desktop .a-price:not(.a-text-price) .a-offscreen',
     '#apex_desktop .a-price:not(.a-text-price) .a-offscreen',
     '#buybox .a-price:not(.a-text-price) .a-offscreen',
     '#newBuyBoxPrice', '#priceblock_ourprice', '#priceblock_dealprice',
     '#price_inside_buybox', '#twister-plus-price'
   ];
   const priceNode = priceSelectors
     .map(selector => [...document.querySelectorAll(selector)].find(node => node.getClientRects().length))
     .find(Boolean) || null;
   const priceContainer = priceNode?.closest('[data-a-currency-code]');
   return {
     currency: priceContainer?.getAttribute('data-a-currency-code') || '',
     price_text: priceNode?.textContent?.trim() || priceNode?.getAttribute('aria-label')?.trim() || ''
   };
 })(),
 offer_kind: document.querySelector('#sns-base-price,.sns-discount') ? 'subscription' :
   document.querySelector('#couponTextpctch,#couponText') ? 'coupon' :
   /\d[\d,.]*\s*[-–]\s*[$£€¥]?\s*\d/.test(document.querySelector('#corePriceDisplay_desktop_feature_div,#corePrice_feature_div')?.innerText || '') ? 'range' : 'current',
 blocked: !!document.querySelector('#captchacharacters,form[action*="validateCaptcha"]'),
 asin: document.querySelector('#ASIN')?.value || '',
 title: document.querySelector('#productTitle')?.textContent?.trim() || '',
 features: [...document.querySelectorAll('#feature-bullets li .a-list-item')].map(e=>e.textContent.trim()).filter(Boolean),
 description: document.querySelector('#productDescription')?.innerText || '',
 image_url: document.querySelector('#landingImage, #imgBlkFront')?.getAttribute('data-old-hires') || document.querySelector('#landingImage, #imgBlkFront')?.src || ''
})"""


logger = logging.getLogger(__name__)
# 紫鸟搜索通道自己持有事件循环。详情必须离开那个循环，否则同步读取会直接失败。
_ISOLATED_FETCHES = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ziniao-product")


def _wait_for_product_body(page: "Page") -> None:
    """以实际商品主体就绪为准；慢脚本和后台标签不能阻塞已到达的资料。"""
    page.wait_for_function(
        """() => !!document.querySelector('#captchacharacters,form[action*="validateCaptcha"]') || (
            !!document.querySelector('#productTitle')?.textContent?.trim() &&
            !!document.querySelector('#ASIN')?.value?.trim()
        )""",
        polling=200,
        timeout=25000,
    )
    if page.evaluate("!!document.querySelector('#captchacharacters,form[action*=validateCaptcha]')"):
        raise SearchFailure("SEARCH_ACCESS_CHECK", "商品页需要人工完成访问验证，请在紫鸟中处理")


def read_product_page(
    page: "Page", marketplace: str, asin: str, *, delivery_ready: bool = False
) -> ProductSnapshot:
    """读取已经打开的商品页。调用方负责打开和关闭标签。"""
    from playwright.sync_api import Error as BrowserError
    from playwright.sync_api import TimeoutError as BrowserTimeout

    url = f"https://{HOSTS[marketplace]}/dp/{asin}"
    try:
        if "/dp/" not in (page.url or ""):
            raise DomainError(503, "AMAZON_PAGE_UNAVAILABLE", "紫鸟商品标签没有打开目标商品")
        if urlsplit(page.url).hostname != HOSTS[marketplace]:
            raise DomainError(502, "ASIN_SITE_CHANGED", "商品页跳转至其他站点，未使用页面资料")
        _wait_for_product_body(page)
        if marketplace == "US":
            zipcode = os.getenv("KEYWORD_US_DELIVERY_ZIP", "10001").strip()
            if not re.fullmatch(r"[0-9]{5}", zipcode):
                raise DomainError(503, "SEARCH_DELIVERY_CONFIG_INVALID", "美国配送邮编必须为5位数字")
            region = page.evaluate("document.querySelector('#glow-ingress-line2')?.innerText || ''")
            try:
                require_us_delivery(region, zipcode)
                confirmed = True
            except DomainError:
                confirmed = False
            # 新会话也先读页面上的配送证据；已确认的邮编不再逐商品强制刷新。
            if not confirmed and not delivery_ready:
                configure_us_delivery(page, zipcode)
                page.reload(wait_until="commit", timeout=25000)
                _wait_for_product_body(page)
                region = page.evaluate("document.querySelector('#glow-ingress-line2')?.innerText || ''")
            require_us_delivery(region, zipcode)
        if urlsplit(page.url).hostname != HOSTS[marketplace]:
            raise DomainError(502, "ASIN_SITE_CHANGED", "商品页跳转至其他站点，未使用页面资料")
        extracted: dict[str, object] = page.evaluate(EXTRACT_PRODUCT)
        if marketplace == "US" and not extracted.get("currency"):
            preferences = [
                str(cookie.get("value", ""))
                for cookie in page.context.cookies([url])
                if cookie.get("name") == "i18n-prefs"
            ]
            if preferences and all(value == "USD" for value in preferences):
                extracted["currency"] = "USD"
        snapshot = parse_product_snapshot(extracted, asin)
        snapshot.source_url = url
        return snapshot
    except DomainError as error:
        if error.code == "ASIN_ACCESS_BLOCKED":
            raise SearchFailure("SEARCH_ACCESS_CHECK", error.message) from None
        raise
    except BrowserTimeout:
        logger.exception("Amazon product page timed out")
        raise DomainError(504, "ASIN_PAGE_TIMEOUT", "Amazon 商品页主体或 ASIN 加载超时，请稍后重试") from None
    except (BrowserError, OSError):
        logger.exception("Amazon product browser failed")
        raise DomainError(
            502, "ASIN_BROWSER_FAILED", "商品页读取超时或浏览器不可用，请检查网络及浏览器配置"
        ) from None


class AmazonProductProvider:
    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or data_directory()

    def fetch(self, marketplace: str, asin: str, *, delivery_ready: bool = False) -> ProductSnapshot:
        """必须由没有 Playwright 事件循环的线程调用。批量入口自己开线程池。"""
        return self._fetch(marketplace, asin, delivery_ready=delivery_ready)

    def fetch_isolated(
        self, marketplace: str, asin: str, *, delivery_ready: bool = False
    ) -> ProductSnapshot:
        """从紫鸟搜索线程调用时使用，避免同步 Playwright 进入已有事件循环。"""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return self._fetch(marketplace, asin, delivery_ready=delivery_ready)
        return _ISOLATED_FETCHES.submit(
            self._fetch, marketplace, asin, delivery_ready=delivery_ready
        ).result()

    def _fetch(self, marketplace: str, asin: str, *, delivery_ready: bool = False) -> ProductSnapshot:
        from playwright.sync_api import Error as BrowserError
        from playwright.sync_api import TimeoutError as BrowserTimeout

        url = f"https://{HOSTS[marketplace]}/dp/{asin}"
        try:
            with amazon_product_page(self.directory, url) as page:
                # 单条与批量详情共用加载、身份和配送校验，避免两条路径再次发生差异。
                return read_product_page(page, marketplace, asin, delivery_ready=delivery_ready)
        except DomainError as error:
            if error.code == "ASIN_ACCESS_BLOCKED":
                raise SearchFailure("SEARCH_ACCESS_CHECK", error.message) from None
            raise
        except BrowserTimeout:
            logger.exception("Amazon product page timed out")
            raise DomainError(
                504, "ASIN_PAGE_TIMEOUT", "Amazon 商品页访问或加载超时，请稍后重试"
            ) from None
        except (BrowserError, OSError):
            logger.exception("Amazon product browser failed")
            raise DomainError(
                502, "ASIN_BROWSER_FAILED", "商品页读取超时或浏览器不可用，请检查网络及浏览器配置"
            ) from None


class AsinProductService:
    def __init__(self, repository: Repository, provider: ProductProvider | None = None) -> None:
        self.repository = repository
        self.provider = provider or AmazonProductProvider()

    def get(self, project: UUID, run: UUID) -> ProductSnapshot | None:
        self.repository.get_run(project, run)
        with self.repository.connect() as conn:
            row = conn.execute(
                "SELECT document FROM asin_products WHERE run_id=?", (str(run),)
            ).fetchone()
        return ProductSnapshot.model_validate_json(row[0]) if row else None

    def resolve(
        self, project: UUID, run: UUID, *, refresh_missing_price: bool = False
    ) -> ProductSnapshot:
        data = self.repository.get_run(project, run).input
        cached = self.get(project, run)
        cached_price_is_reliable = bool(
            cached and cached.price is not None and cached.currency and cached.price_reliable
        )
        if cached and (not refresh_missing_price or cached_price_is_reliable):
            return cached
        if not data.asin:
            raise DomainError(422, "ASIN_REQUIRED", "当前任务未提供 ASIN")
        result = self.provider.fetch(data.marketplace, data.asin)
        if result.asin != data.asin or not result.title.strip():
            raise DomainError(
                502,
                "ASIN_IDENTITY_UNCONFIRMED",
                f"资料供应商返回的商品身份为 {result.asin}，与输入 {data.asin} 不匹配；请核对输入 ASIN 是否为可售子体",
            )
        with self.repository.connect() as conn:
            if cached:
                conn.execute(
                    "UPDATE asin_products SET document=? WHERE run_id=?",
                    (result.model_dump_json(), str(run)),
                )
            else:
                conn.execute(
                    "INSERT INTO asin_products (run_id, document) VALUES (?,?) ON CONFLICT (run_id) DO NOTHING",
                    (str(run), result.model_dump_json()),
                )
        return self.get(project, run) or result
