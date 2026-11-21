"""迁入原 ABA 工作台的紫鸟恢复机制；凭据与端口只写入当前项目。"""

import json
import re
import time
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, cast
from urllib.parse import urlparse

from app.adapters.ziniao import ensure_port as ensure_ziniao_port
from app.adapters.ziniao import listening_ports, probe_port
from app.domain.errors import DomainError

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Page


class AbaBrowserSession:
    def __init__(
        self,
        directory: Path,
        *,
        port: int | None = None,
        auth_path: Path | None = None,
    ) -> None:
        self.directory = directory
        self.auth_path = auth_path or directory / "aba_auth.json"
        self.port = port
        self.lock = RLock()
        self.last_refresh = float("-inf")
        self.last_start = float("-inf")

    def _probe(self, port: int) -> bool:
        """只探测本机监听端口；识别原项目使用的紫鸟 Chrome/129。"""
        return probe_port(port)

    def _listening_ports(self) -> list[int]:
        return listening_ports()

    def discover_port(self) -> int | None:
        """优先显式端口、上次发现端口，失效后重找；多个候选不任意选店铺。"""
        with self.lock:
            candidates = [self.port]
            try:
                value = (self.directory / "cdp_port").read_text(encoding="utf-8").strip()
                if value.isdecimal():
                    candidates.append(int(value))
            except OSError:
                pass
            candidates.append(38979)
            for port in dict.fromkeys(candidates):
                if port and 1 <= port <= 65535 and self._probe(port):
                    self.port = port
                    return port
            ports = self._listening_ports()
            with ThreadPoolExecutor(max_workers=8) as pool:
                matches = [
                    port for port, ok in zip(ports, pool.map(self._probe, ports), strict=True) if ok
                ]
            if len(matches) > 1:
                raise DomainError(
                    503,
                    "ABA_BROWSER_AMBIGUOUS",
                    "发现多个紫鸟浏览器，请配置目标店铺的 KEYWORD_ABA_CDP_PORT",
                )
            if not matches:
                return None
            self.port = matches[0]
            self.directory.mkdir(parents=True, exist_ok=True)
            (self.directory / "cdp_port").write_text(str(self.port), encoding="utf-8")
            return self.port

    def ensure_port(self) -> int:
        """复用已打开店铺；没有 CDP 时自动以 agent 模式启动目标店铺。"""
        with self.lock:
            port = self.discover_port()
            if port is not None:
                return port
            port = ensure_ziniao_port(self.directory, "KEYWORD_ABA_CDP_PORT")
            self.port = port
            return port

    @contextmanager
    def _page(self) -> Generator[tuple["BrowserContext", "Page"]]:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright

        with self.lock:
            port = self.ensure_port()
            try:
                with sync_playwright() as playwright:
                    browser = playwright.chromium.connect_over_cdp(
                        f"http://127.0.0.1:{port}", timeout=10000
                    )
                    contexts = browser.contexts
                    pages = [
                        (context, page)
                        for context in contexts
                        for page in context.pages
                        if urlparse(page.url).hostname == "sellercentral.amazon.com"
                    ]
                    if not pages:
                        if len(contexts) != 1:
                            raise DomainError(
                                503,
                                "ABA_BROWSER_AMBIGUOUS",
                                "无法确定目标店铺，请在紫鸟打开美国站卖家后台",
                            )
                        page = contexts[0].new_page()
                        page.goto(
                            "https://sellercentral.amazon.com/",
                            wait_until="domcontentloaded",
                            timeout=35000,
                        )
                        pages = [(contexts[0], page)]
                    context, page = pages[0]
                    if len({id(ctx) for ctx, _ in pages}) > 1:
                        raise DomainError(
                            503,
                            "ABA_BROWSER_AMBIGUOUS",
                            "浏览器内存在多个店铺会话，请单独连接目标店铺",
                        )
                    url = urlparse(page.url)
                    if url.hostname != "sellercentral.amazon.com" or url.path.startswith("/ap/"):
                        raise DomainError(503, "ABA_AUTH_REQUIRED", "请在紫鸟中登录美国站卖家后台")
                    yield context, page
                    # 退出只断开自动化连接，不能关闭用户浏览器或页面。
            except PlaywrightError:
                self.port = None
                raise DomainError(
                    503,
                    "ABA_CDP_UNAVAILABLE",
                    "紫鸟卖家后台连接或页面请求失败，请检查店铺网络和登录状态",
                ) from None

    @contextmanager
    def page(self) -> Generator[tuple["BrowserContext", "Page"]]:
        """供其他采集器复用现有会话；生命周期仍由本适配器管理。"""
        with self._page() as session:
            yield session

    def refresh_auth(self) -> None:
        """只保存所选会话的 Amazon Cookie，不输出 Cookie，也不回写原项目。"""
        with self.lock:
            if time.monotonic() - self.last_refresh < 60:
                return
            self.last_refresh = time.monotonic()
            with self._page() as (context, page):
                cookies = [
                    cookie
                    for cookie in context.cookies()
                    if re.search(r"(^|\.)amazon\.com$", cookie.get("domain", ""))
                    and cookie.get("name")
                    and cookie.get("value")
                ]
                if not cookies:
                    raise DomainError(
                        503, "ABA_AUTH_REQUIRED", "紫鸟中没有美国站登录凭据，请先登录"
                    )
                ua: str = page.evaluate("navigator.userAgent")
                csrf: str = page.evaluate(
                    "document.querySelector('meta[name=anti-csrftoken-a2z]')?.content || ''"
                )
                data = {
                    "cookie": "; ".join(
                        f"{c.get('name', '')}={c.get('value', '')}" for c in cookies
                    ),
                    "ua": ua,
                    "csrf": csrf,
                    "cookieCount": len(cookies),
                    "exportedAt": datetime.now(UTC).isoformat(),
                    "source": "ziniao",
                }
                self.auth_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.auth_path.with_suffix(".tmp")
                try:
                    temporary.write_text(json.dumps(data), encoding="utf-8")
                    temporary.replace(self.auth_path)
                finally:
                    temporary.unlink(missing_ok=True)

    def request(self, url: str, body: dict[str, object]) -> tuple[int, str]:
        with self._page() as (_, page):
            result: object = page.evaluate(
                """async ({url, body}) => {
                    const r = await fetch(url, {method: 'POST', credentials: 'include',
                        headers: {'Content-Type': 'application/json', 'Accept': 'application/json',
                                  'X-Requested-With': 'XMLHttpRequest'},
                        body: JSON.stringify(body), signal: AbortSignal.timeout(35000)});
                    return {status: r.status, text: await r.text()};
                }""",
                {"url": url, "body": body},
            )
            if not isinstance(result, dict):
                raise DomainError(502, "ABA_INVALID_RESPONSE", "紫鸟返回数据结构异常")
            data = cast(dict[str, object], result)
            status, text = data.get("status"), data.get("text")
            if not isinstance(status, int) or not isinstance(text, str):
                raise DomainError(502, "ABA_INVALID_RESPONSE", "紫鸟返回数据结构异常")
            return status, text
