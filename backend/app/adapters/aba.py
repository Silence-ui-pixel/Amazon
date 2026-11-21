"""迁自 Node ABA 工作台的协议适配：直连优先，可选本地已登录 CDP 兜底。"""

import hashlib
import json
import time
from collections.abc import Callable
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal, Protocol, cast
from urllib.parse import urlparse

import httpx

from app.adapters.aba_browser import AbaBrowserSession
from app.adapters.browser_port import resolve_cdp_port
from app.domain.aba import AbaConnectionStatus, AbaProduct, AbaQueryReport, AbaRow
from app.domain.errors import DomainError
from app.domain.traffic import MetricInput, metric_snapshot

BASE = "https://sellercentral.amazon.com/api/brand-analytics/v1/dashboard/top-search-terms"
REPORT_ID = "top-search-terms-report-table"


def invalid_response() -> DomainError:
    return DomainError(
        502, "ABA_INVALID_RESPONSE", "ABA 返回结构异常，请检查登录状态或接口是否变化"
    )


def decode_response(status: int, text: str) -> dict[str, object]:
    """不回传供应商响应正文，避免登录页或调试内容泄露。"""
    if status == 401:
        raise DomainError(503, "ABA_AUTH_REQUIRED", "ABA 登录已失效，请更新本项目登录凭据")
    if status == 403:
        raise DomainError(503, "ABA_ACCESS_DENIED", "ABA 拒绝访问，请检查品牌分析权限和登录状态")
    if status == 429:
        raise DomainError(503, "ABA_RATE_LIMITED", "ABA 请求受到限流，请稍后再试")
    if status != 200:
        raise DomainError(502, "ABA_UPSTREAM_ERROR", "ABA 服务暂时不可用")
    try:
        value: object = json.loads(text)
    except ValueError:
        raise invalid_response() from None
    if not isinstance(value, dict):
        raise invalid_response()
    return cast(dict[str, object], value)


def normalize_row(raw: object) -> AbaRow:
    """原协议的份额单位是百分数，统一转换为 0..1；无效排名不能默认为零。"""
    if not isinstance(raw, dict):
        raise invalid_response()
    item = cast(dict[str, object], raw)
    term, rank = item.get("st-search-term"), item.get("st-search-frequency")
    if not isinstance(term, str) or not term.strip() or isinstance(rank, bool):
        raise invalid_response()
    rank_text = str(rank)
    if not rank_text.isascii() or not rank_text.isdecimal() or int(rank_text) <= 0:
        raise invalid_response()

    def share(key: str) -> float | None:
        value = item.get(key)
        if value is None or value == "" or value == "-":
            return None
        try:
            number = float(str(value).rstrip("%")) / 100
        except ValueError:
            raise invalid_response() from None
        if not 0 <= number <= 1:
            raise invalid_response()
        return number

    products: list[AbaProduct] = []
    for position in range(1, 4):
        asin = item.get(f"st-top-asin-{position}")
        if isinstance(asin, str) and asin.strip() and asin != "-":
            title = item.get(f"st-top-asin-title-{position}")
            products.append(
                AbaProduct(
                    asin=asin,
                    title=title if isinstance(title, str) else None,
                    click_share=share(f"st-top-asin-click-share-{position}"),
                    conversion_share=share(f"st-top-asin-conversion-share-{position}"),
                )
            )
    def metric_share(position: int, field: str) -> Decimal | None:
        asin = item.get(f"st-top-asin-{position}")
        if not isinstance(asin, str) or not asin.strip() or asin == "-":
            return None
        value = item.get(f"st-top-asin-{field}-{position}")
        if value is None or value == "" or value == "-":
            return None
        try:
            number = Decimal(str(value).removesuffix("%")) / 100
        except InvalidOperation:
            raise invalid_response() from None
        if not number.is_finite() or not 0 <= number <= 1:
            raise invalid_response()
        return number

    rank_value = int(rank_text)
    return AbaRow(
        keyword=term.strip(),
        search_frequency_rank=rank_value,
        products=products,
        traffic_metrics=metric_snapshot(
            MetricInput(
                rank=rank_value,
                click=metric_share(1, "click-share"),
                conversion=metric_share(1, "conversion-share"),
                conversion2=metric_share(2, "conversion-share"),
                conversion3=metric_share(3, "conversion-share"),
            )
        ),
    )


class AbaProvider(Protocol):
    """ABA 周报抓取的最小接口；本地直连与远程 Agent 客户端都实现它。"""

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
    ) -> AbaQueryReport: ...

    def status(self) -> AbaConnectionStatus: ...


class AbaClient:
    """无导入副作用；凭据每次请求加载，更新本项目凭据后无需改动源项目。"""

    def __init__(
        self,
        auth_path: Path,
        *,
        transport: httpx.BaseTransport | None = None,
        attempts: int = 3,
        retry_delay: float = 1.2,
        cdp_port: int | None = None,
        browser_session: AbaBrowserSession | None = None,
    ) -> None:
        self.auth_path = auth_path
        self.transport = transport
        self.attempts = max(1, min(attempts, 6))
        self.retry_delay = retry_delay
        self.cdp_port = cdp_port
        self.browser_session = browser_session
        self.direct_failed_until = 0.0

    @classmethod
    def from_directory(cls, directory: Path) -> "AbaClient":
        # 只读取本项目的独立副本，运行时不再依赖桌面源目录。
        port = resolve_cdp_port(directory, "KEYWORD_ABA_CDP_PORT", default=None)
        if port is None:
            # ABA 周报和前台搜索用的是同一个紫鸟店铺：本目录没记住端口时，
            # 回退到 amazon 目录记住的那个，避免多店铺时扫出「店铺不明确」。
            port = resolve_cdp_port(
                directory.parent / "amazon", "KEYWORD_ABA_CDP_PORT", default=None
            )
        return cls(
            directory / "aba_auth.json",
            cdp_port=port,
            browser_session=AbaBrowserSession(directory, port=port),
        )

    def download_original(self, request: dict, target: Path) -> tuple[str, bytes]:
        from app.adapters.aba_platform_export import download_original
        return download_original(self.browser_session, request, target)

    def _headers(self) -> dict[str, str]:
        try:
            value: object = json.loads(self.auth_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            raise DomainError(503, "ABA_AUTH_REQUIRED", "请先在本项目配置 ABA 登录凭据") from None
        if not isinstance(value, dict):
            raise DomainError(503, "ABA_AUTH_REQUIRED", "ABA 登录凭据格式不正确")
        auth = cast(dict[str, object], value)
        cookie = auth.get("cookie")
        if not isinstance(cookie, str) or not cookie.strip() or "\n" in cookie or "\r" in cookie:
            raise DomainError(503, "ABA_AUTH_REQUIRED", "ABA 登录凭据缺少有效 Cookie")
        ua = auth.get("ua")
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": ua
            if isinstance(ua, str) and "\n" not in ua and "\r" not in ua
            else "Mozilla/5.0",
            "Cookie": cookie,
        }

    def status(self) -> AbaConnectionStatus:
        try:
            self._headers()
            present = True
        except DomainError:
            present = False
        return AbaConnectionStatus(
            credentials_present=present, cdp_configured=self.cdp_port is not None
        )

    def _direct(self, body: dict[str, object]) -> dict[str, object]:
        headers = self._headers()
        with httpx.Client(transport=self.transport, timeout=35, follow_redirects=False) as client:
            for attempt in range(self.attempts):
                try:
                    response = client.post(BASE + "/reports", headers=headers, json=body)
                    if (
                        response.status_code in (429, 500, 502, 503, 504)
                        and attempt + 1 < self.attempts
                    ):
                        time.sleep(self.retry_delay * (attempt + 1))
                        continue
                    return decode_response(response.status_code, response.text)
                except httpx.RequestError:
                    if attempt + 1 == self.attempts:
                        raise DomainError(
                            502, "ABA_NETWORK_ERROR", "连接 ABA 超时或网络不可用"
                        ) from None
                    time.sleep(self.retry_delay * (attempt + 1))
        raise DomainError(502, "ABA_NETWORK_ERROR", "无法连接 ABA")

    def _via_cdp(self, body: dict[str, object]) -> dict[str, object]:
        if self.browser_session is not None:
            status, text = self.browser_session.request(BASE + "/reports", body)
            return decode_response(status, text)
        # 保留显式构造客户端的兼容通道；生产环境使用上面的自动恢复会话。
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright

        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.connect_over_cdp(
                    f"http://127.0.0.1:{self.cdp_port}", timeout=10000
                )
                pages = [
                    page
                    for context in browser.contexts
                    for page in context.pages
                    if urlparse(page.url).hostname == "sellercentral.amazon.com"
                ]
                if not pages:
                    raise DomainError(
                        503, "ABA_CDP_PAGE_REQUIRED", "请在已连接的浏览器中打开美国站卖家后台"
                    )
                result: object = pages[0].evaluate(
                    """async ({url, body}) => {
                        const r = await fetch(url, {
                            method: 'POST', credentials: 'include',
                            headers: {'Content-Type': 'application/json', 'Accept': 'application/json',
                                      'X-Requested-With': 'XMLHttpRequest'},
                            body: JSON.stringify(body), signal: AbortSignal.timeout(35000)
                        });
                        return {status: r.status, text: await r.text()};
                    }""",
                    {"url": BASE + "/reports", "body": body},
                )
                # sync_playwright 退出只断开本连接；不能 browser.close() 关掉用户登录会话。
                if not isinstance(result, dict):
                    raise invalid_response()
                data = cast(dict[str, object], result)
                status, text = data.get("status"), data.get("text")
                if not isinstance(status, int) or not isinstance(text, str):
                    raise invalid_response()
                return decode_response(status, text)
        except PlaywrightError:
            raise DomainError(
                503, "ABA_CDP_UNAVAILABLE", "无法使用已配置的本地卖家后台浏览器"
            ) from None

    # 优先使用保存凭据直连；可恢复的登录/网络错误才转紫鸟会话，限流不切换绕过。
    # 凭据过期后的恢复依赖后端同机已登录浏览器，单独迁移凭据文件不能保证长期采集。
    def _request(self, body: dict[str, object]) -> dict[str, object]:
        if self.browser_session is not None and time.monotonic() < self.direct_failed_until:
            return self._via_cdp(body)
        try:
            result = self._direct(body)
            self.direct_failed_until = 0
            return result
        except DomainError as error:
            # 限流不切通道；数据协议错误也不能悄悄替换成另一份结果。
            recoverable = {
                "ABA_AUTH_REQUIRED",
                "ABA_ACCESS_DENIED",
                "ABA_NETWORK_ERROR",
                "ABA_UPSTREAM_ERROR",
            }
            if error.code not in recoverable:
                raise
            if self.browser_session is not None:
                if error.code in ("ABA_AUTH_REQUIRED", "ABA_ACCESS_DENIED"):
                    # 过期 Cookie 可能返回 401 也可能 403；两种都先刷新凭据重试直连。
                    try:
                        self.browser_session.refresh_auth()
                        return self._direct(body)
                    except DomainError as refresh_error:
                        if refresh_error.code not in recoverable:
                            raise
                # 原项目的短期通道记忆：断网后分页不再反复等待直连超时。
                if error.code == "ABA_NETWORK_ERROR":
                    self.direct_failed_until = time.monotonic() + 180
                return self._via_cdp(body)
            if self.cdp_port is None:
                raise
            return self._via_cdp(body)

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
        if country != "us":
            raise DomainError(422, "ABA_SITE_UNSUPPORTED", "本次迁入只验证美国站 ABA")
        if (
            not query.strip()
            or not 1 <= max_rows <= 10000
            or query_mode not in ("root", "asin", "product_term")
            or (rank_limit is not None and rank_limit < 1)
        ):
            raise DomainError(422, "ABA_QUERY_INVALID", "ABA 查询条件或采集上限不正确")
        try:
            day = date.fromisoformat(week_end)
        except ValueError:
            raise DomainError(422, "ABA_WEEK_INVALID", "周结束日格式不正确") from None
        if day.weekday() != 5:
            raise DomainError(422, "ABA_WEEK_INVALID", "ABA 周报结束日应为周六")
        rows: list[AbaRow] = []
        raw_rows: list[dict[str, object]] = []
        seen: set[str] = set()
        seen_keywords: set[str] = set()
        total = 0
        reason: Literal[
            "complete", "rank_limit", "row_limit", "page_limit", "short_page", "repeated_page"
        ] = "page_limit"
        page = 0
        previous_rank = 0

        def snapshot() -> AbaQueryReport:
            result = AbaQueryReport(
                query=query,
                query_mode=query_mode,
                total=total,
                fetched=len(rows),
                pages=page,
                truncated=reason not in ("complete", "rank_limit"),
                stop_reason=reason,
                rows=rows,
            )
            result.raw_rows = list(raw_rows)
            return result

        for page in range(1, 121):
            filters = [
                {"id": "reporting-range", "value": "weekly", "valueType": None},
                {"id": "weekly-week", "value": week_end, "valueType": "weekly"},
                {
                    "id": "asins" if query_mode == "asin" else "search-term-freeform",
                    "value": query,
                    "valueType": None,
                },
            ]
            payload = self._request(
                {
                    "viewId": "top-search-terms-default-view",
                    "filterSelections": filters,
                    "reportOperations": [
                        {
                            "reportId": REPORT_ID,
                            "reportType": "TABLE",
                            "pageNumber": page,
                            "pageSize": 100,
                            "sortByColumnId": "st-search-frequency",
                            "ascending": True,
                        }
                    ],
                    "selectedCountries": [country],
                    "reportId": REPORT_ID,
                }
            )
            reports = payload.get("reportsV2")
            if not isinstance(reports, list) or not reports or not isinstance(reports[0], dict):
                raise invalid_response()
            report = cast(dict[str, object], reports[0])
            count, batch_raw = report.get("totalItems"), report.get("rows")
            if (
                isinstance(count, bool)
                or not isinstance(count, int)
                or count < 0
                or not isinstance(batch_raw, list)
            ):
                raise invalid_response()
            batch = cast(list[object], batch_raw)
            # 同一次分页总数变化时不混合出一份假完整的周报。
            if page > 1 and count != total:
                raise DomainError(502, "ABA_REPORT_CHANGED", "ABA 分页期间总数变化，请重新采集")
            total = count
            if len(rows) + len(batch) > total or len(batch) > 100:
                raise invalid_response()
            signature = hashlib.sha256(json.dumps(batch, sort_keys=True).encode()).hexdigest()
            if batch and signature in seen:
                reason = "repeated_page"
                break
            seen.add(signature)
            normalized_batch = [normalize_row(item) for item in batch]
            # normalize_row has already rejected every non-dict item in this page.
            raw_batch = [cast(dict[str, object], item) for item in batch]
            if rank_limit is not None:
                ranks = [item.search_frequency_rank for item in normalized_batch]
                if any(right < left for left, right in zip(ranks, ranks[1:], strict=False)) or (
                    ranks and ranks[0] < previous_rank
                ):
                    raise DomainError(
                        502,
                        "ABA_RANK_ORDER_INVALID",
                        "ABA 搜索频率排名未按升序返回，不能安全使用 25 万边界",
                    )
                if ranks:
                    previous_rank = ranks[-1]
                within = [
                    (raw, normalized)
                    for raw, normalized in zip(raw_batch, normalized_batch, strict=True)
                    if normalized.search_frequency_rank <= rank_limit
                ]
                reached_rank_limit = len(within) < len(batch)
            else:
                within = list(zip(raw_batch, normalized_batch, strict=True))
                reached_rank_limit = False
            available = within[: max_rows - len(rows)]
            overlap = False
            for item, normalized in available:
                keyword = " ".join(normalized.keyword.split()).casefold()
                if keyword in seen_keywords:
                    overlap = True
                # 重叠只改变完整性，不丢弃可能不同的排名、商品或原始字段。
                seen_keywords.add(keyword)
                rows.append(normalized)
                raw_rows.append(item)
            if overlap:
                reason = "repeated_page"
            elif reached_rank_limit:
                reason = "rank_limit"
            elif len(rows) >= total:
                reason = "complete"
            elif len(rows) >= max_rows:
                reason = "row_limit"
            elif len(batch) < 100:
                reason = "short_page"
            if on_progress is not None:
                on_progress(snapshot())
            if reason != "page_limit":
                break
            time.sleep(0.18)
        return snapshot()
