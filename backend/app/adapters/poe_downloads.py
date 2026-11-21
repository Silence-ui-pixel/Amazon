"""商机商品原件归档；校验通过才发布原始字节和来源清单。"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING, Protocol, cast
from urllib.parse import urlparse, urlsplit, urlunsplit
from uuid import UUID, uuid4

from app.adapters.competitor_csv import parse_competitor_csv
from app.domain.competitors import Competitor, PoeIntentCandidate
from app.domain.errors import DomainError

if TYPE_CHECKING:
    from playwright.sync_api import Page


@dataclass(frozen=True)
class PoeArchive:
    id: UUID
    path: Path
    text: str
    sha256: str
    items: list[Competitor]
    original_filename: str
    source_url: str
    export_metadata: dict[str, object] = field(default_factory=dict[str, object])


class PoeDownloadStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def archive(
        self, source: Path, run_id: UUID, keyword: str, filename: str, source_url: str,
        *, export_metadata: dict[str, object] | None = None,
    ) -> PoeArchive:
        """文件来自浏览器下载临时目录；不使用供应商文件名构造保存路径。"""
        url = urlparse(source_url)
        if (
            not filename
            or PureWindowsPath(filename).name != filename
            or Path(filename).name != filename
            or Path(filename).suffix.casefold() != ".csv"
            or url.scheme != "https"
            or url.hostname != "sellercentral.amazon.com"
            or not url.path.startswith("/opportunity-explorer/")
        ):
            raise DomainError(422, "POE_DOWNLOAD_INVALID", "请下载美国站商机探测器的商品 CSV")
        if not 0 < source.stat().st_size <= 2_000_000:
            raise DomainError(422, "POE_DOWNLOAD_SIZE", "商品 CSV 须为 1 字节至 2 MB")
        raw = source.read_bytes()
        try:
            # 保留原始字节；仅解析副本。Excel 导出可能带 UTF-8 或 UTF-16 BOM。
            encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
            text = raw.decode(encoding)
        except UnicodeError:
            raise DomainError(422, "POE_DOWNLOAD_ENCODING", "商品 CSV 编码无法识别") from None
        items = parse_competitor_csv(text, filename)
        artifact_id = uuid4()
        digest = hashlib.sha256(raw).hexdigest()
        directory = self.directory / str(run_id)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{artifact_id}.csv"
        manifest = {
            "id": str(artifact_id),
            "run_id": str(run_id),
            "keyword": keyword,
            "original_filename": filename,
            "source_url": source_url,
            "captured_at": datetime.now(UTC).isoformat(),
            "sha256": digest,
            "size": len(raw),
            "rows": len(items),
            "export_metadata": export_metadata or {},
        }
        # 写完两份临时文件再发布；失败不留下看似可用的原件。
        partial = target.with_suffix(".part")
        metadata = target.with_suffix(".json")
        try:
            partial.write_bytes(raw)
            metadata.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            partial.replace(target)
        except OSError:
            partial.unlink(missing_ok=True)
            metadata.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            raise DomainError(
                500, "POE_ARCHIVE_FAILED", "商品 CSV 保存失败，请检查本地存储"
            ) from None
        return PoeArchive(artifact_id, target, text, digest, items, filename, source_url, export_metadata or {})


def competitor_export_filename(keyword: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", keyword).strip().rstrip(". ")
    return f"筛选竞对ASIN-{cleaned[:100] or 'products'}.csv"


def parse_poe_intent_rows(rows: list[dict[str, object]]) -> list[PoeIntentCandidate]:
    results: list[PoeIntentCandidate] = []
    seen: set[str] = set()
    try:
        for row in rows:
            parsed = urlsplit(str(row.get("ref", "")))
            if parsed.scheme not in ("", "https") or parsed.netloc not in ("", "sellercentral.amazon.com"):
                raise ValueError("越域购买意图")
            if not re.fullmatch(r"/opportunity-explorer/(?:market-demand/purchase-intent|explore/niche)/[a-zA-Z0-9-]+", parsed.path):
                raise ValueError("购买意图地址无效")
            ref = urlunsplit(("", "", parsed.path, parsed.query, ""))
            if ref in seen:
                continue
            item = PoeIntentCandidate.model_validate({**row, "ref": ref})
            if not item.name.strip():
                raise ValueError("购买意图缺少名称")
            seen.add(ref)
            results.append(item)
        if not results or len(results) > 100:
            raise ValueError("缺少购买意图或数量异常")
        return results
    except ValueError:
        raise DomainError(422, "POE_INTENT_RESULTS_INVALID", "商机购买意图列表缺失或地址无效") from None


class PoeDownloader(Protocol):
    """商机探测器 CSV 下载的最小接口；本地实现与远程 Agent 客户端都实现它。"""

    def intents(self, keyword: str) -> list[PoeIntentCandidate]: ...

    def download(self, run_id: UUID, keyword: str, intent_ref: str = "") -> PoeArchive: ...


class PoeBrowserDownloader:
    """通过现有紫鸟会话操作真实页面；只关闭本次创建的页面。"""

    def __init__(self, directory: Path, store: PoeDownloadStore) -> None:
        from app.adapters.aba_browser import AbaBrowserSession

        self.session = AbaBrowserSession(directory)
        self.store = store

    def intents(self, keyword: str) -> list[PoeIntentCandidate]:
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        try:
            with self.session.page() as (context, _existing):
                page = context.new_page()
                try:
                    page.goto(
                        "https://sellercentral.amazon.com/opportunity-explorer/market-demand?obfuscatedMarketplaceId=ATVPDKIKX0DER",
                        wait_until="domcontentloaded",
                        timeout=60000,
                    )
                    page.bring_to_front()
                    search = page.get_by_role("textbox", name=re.compile(r"输入关键词或 ASIN|Enter.*keyword.*ASIN", re.I))
                    search.fill(keyword, timeout=30000)
                    search.press("Enter")
                    page.wait_for_url("**/market-demand/search?**", timeout=30000)
                    links = page.locator("table a[href*='/purchase-intent/']")
                    links.first.wait_for(timeout=30000)
                    rows = links.evaluate_all("""links => links.map(a => {
                        const row=a.closest('tr'), table=a.closest('table');
                        const cells=[...row.querySelectorAll('td')];
                        const head=[...table.querySelectorAll('thead tr')].at(-1);
                        const headers=head ? [...head.children].map(x=>x.innerText.trim()) : [];
                        return {ref:a.getAttribute('href'),name:a.innerText.trim(),
                            top_keywords:(cells[1]?.innerText||'').split(',').map(x=>x.trim()).filter(Boolean).slice(0,30),
                            metrics:Object.fromEntries(cells.slice(2).map((c,i)=>[headers[i+2]||('column_'+(i+2)),c.innerText.trim()]))};
                    })""")
                    return parse_poe_intent_rows(rows)
                finally:
                    page.close()
        except PlaywrightTimeout:
            raise DomainError(504, "POE_SEARCH_TIMEOUT", "商机购买意图搜索超时，请检查卖家后台登录与网络") from None

    @staticmethod
    def read_export(page: "Page") -> dict[str, object]:
        """读取页面下载锚点已生成的原始 Blob，不重建 CSV、不依赖浏览器下载弹窗。"""
        export = page.locator('a[data-testid="DownloadButton"]')
        export.wait_for(timeout=30000)
        return cast(dict[str, object], export.evaluate("""async a => {
            if (!a.href.startsWith('blob:https://sellercentral.amazon.com/')) throw Error('Unexpected export origin');
            const response=await fetch(a.href);
            if(!response.ok) throw Error('Export read failed');
            const buffer=await response.arrayBuffer();
            if(!buffer.byteLength || buffer.byteLength>2000000) throw Error('Export size invalid');
            return {bytes:[...new Uint8Array(buffer)],browser_filename:a.download,
                time_window:document.querySelector('kat-dropdown[data-testid="MarketDemandProductsTimeWindowDropdown"]')?.getAttribute('value'),
                displayed_headers:[...document.querySelectorAll('table th')].map(x=>x.innerText)};
        }"""))

    def download(self, run_id: UUID, keyword: str, intent_ref: str = "") -> PoeArchive:
        from tempfile import TemporaryDirectory
        from playwright.sync_api import TimeoutError as PlaywrightTimeout

        if not intent_ref:
            candidates = self.intents(keyword)
            if len(candidates) != 1:
                raise DomainError(422, "POE_NICHE_SELECTION_REQUIRED", "请先选择与商品最相关的购买意图")
            intent_ref = candidates[0].ref
        chosen = parse_poe_intent_rows([{"ref": intent_ref, "name": keyword}])[0]
        parts = urlsplit(chosen.ref)
        url = "https://sellercentral.amazon.com" + urlunsplit(("", "", parts.path + "/product", parts.query, ""))
        try:
            with self.session.page() as (context, _existing):
                page = context.new_page()
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    page.bring_to_front()
                    control = page.locator('kat-dropdown[data-testid="MarketDemandProductsTimeWindowDropdown"]')
                    control.wait_for(timeout=30000)
                    if control.get_attribute("value") != "T30":
                        if control.get_attribute("expanded") is None:
                            control.click()
                        control.get_by_role("option", name=re.compile(r"^(过去 30 天|Past 30 days|Last 30 days)$", re.I)).click()
                    page.wait_for_function("document.querySelector('kat-dropdown[data-testid=MarketDemandProductsTimeWindowDropdown]')?.getAttribute('value') === 'T30'", timeout=20000)
                    page.get_by_role("columnheader", name=re.compile(r"(?:平均.*售价.*30|Average.*Selling.*Price.*30)", re.I | re.S)).wait_for(timeout=20000)
                    payload = self.read_export(page)
                    if payload.get("time_window") != "T30":
                        raise DomainError(422, "POE_30_DAY_REQUIRED", "商品表未切换到过去 30 天")
                    raw = bytes(cast(list[int], payload.pop("bytes")))
                    encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
                    filename = competitor_export_filename(keyword)
                    parse_competitor_csv(raw.decode(encoding), filename, require_30_day=True)
                    with TemporaryDirectory(prefix="keyword-poe-") as temporary:
                        source = Path(temporary) / "download.tmp"
                        source.write_bytes(raw)
                        return self.store.archive(source, run_id, keyword, filename, page.url, export_metadata=payload)
                finally:
                    page.close()
        except PlaywrightTimeout:
            raise DomainError(
                504,
                "POE_DOWNLOAD_TIMEOUT",
                "商机 30 天商品页面或原件读取超时，请检查紫鸟登录、网络后重试",
            ) from None
