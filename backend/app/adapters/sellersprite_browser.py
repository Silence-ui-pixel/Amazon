"""通过已登录 Chrome 操作卖家精灵查询及浏览器原件下载。"""

import contextlib
import json
import os
import re
import socket
import time
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page
from urllib.parse import urlsplit

from app.adapters.xlsx_simple import build_xlsx
from app.adapters.ziniao import ensure_port
from app.domain.errors import DomainError
from app.domain.sellersprite import BrowserCollectionInput, CollectionMethod, VariantScope

KEYWORD_PLACEHOLDER = re.compile(r"flashlight", re.IGNORECASE)
ASIN_PLACEHOLDER = re.compile(r"B00FLYWNYQ", re.IGNORECASE)
QUERY_BUTTON = re.compile(r"^(立即查询|Search)$", re.IGNORECASE)
EXPORT_BUTTON = re.compile(r"^\s*(导出|Export( Keywords)?)\s*$", re.IGNORECASE)
RESULT_COUNT = re.compile(r"(?:搜索结果数|Search Results?)\s*[:：]\s*([\d,]+)", re.IGNORECASE)
VIEW_EXPORT = re.compile(r"前往查看|View (now|exports?)|Go to", re.IGNORECASE)
CHINESE_OPTION = re.compile(r"中文|简体|Chinese", re.IGNORECASE)
CUSTOM_COLUMNS = re.compile(r"^\s*(自定义列|Custom Columns?)\s*$", re.IGNORECASE)
SAVE_COLUMNS = re.compile(r"^\s*(保存|Save)\s*$", re.IGNORECASE)
US_MARKETPLACES = frozenset({"美国站", "United States"})
MARKETPLACE_LABELS = US_MARKETPLACES | {"美国", "US", "USA"}
PERIOD_OPTION = re.compile(r"最近\s*30\s*天|近\s*30\s*天|Last 30 days", re.IGNORECASE)
PERIOD_LABELS = ("最近30天", "近30天", "Last 30 days", "Last 30 Days")
# 拓展流量词走“我的导出”异步任务，真实文件名形如
# ExpandKeywords-US-B0HGB5ZR95-batch(19)-202611-25095.xlsx：
# 只带第一个 ASIN 和批量条数，不带全部 ASIN，也不带 30 天标记（周期须在页面上核对）。
TRAFFIC_EXPORT_NAME = re.compile(
    r"^ExpandKeywords-US-(?P<asin>[A-Za-z0-9]+)-batch\((?P<count>\d+)\)-\d{6}-\d+\.xlsx$",
    re.IGNORECASE,
)
TRAFFIC_EXPORT_PREFIX = "ExpandKeywords-US-"
KEYWORD_MINER_PREFIX = "KeywordMining-US-"
# v3 页面（如拓展流量词）把 html lang 留在 en，按钮文案也和旧页不同；
# 用可见正文的中文字符量判断真实界面语言，避免把中文页误判成英文。
CJK = re.compile("[㐀-䶿一-鿿]")
CJK_THRESHOLD = 10
LOGIN_HINT = re.compile(r"登录|登陆|sign in|log in|password", re.IGNORECASE)
LANGUAGE_TRIGGERS = (
    ".language-trigger",
    ".nation",
    ".el-dropdown-selfdefine",
    ".lang-switch",
    ".language-switch",
)
LANGUAGE_MENU_SCOPES = (
    ".el-dropdown-menu:visible",
    "[role=menu]:visible",
    ".language-menu:visible",
    ".lang-menu:visible",
)
LANGUAGE_ITEMS = (
    "button[role=menuitemradio]",
    "button[role=menuitem]",
    "[role=menuitem]",
    ".el-dropdown-menu__item",
    "li.lang-item",
    "li",
)


def collection_path(method: CollectionMethod) -> str:
    return {
        "keyword_mining": "/v3/keyword-miner",
        "keyword_reverse": "/v3/keyword-reverse",
        "traffic_extension": "/v3/traffic/extend",
    }[method]


def variant_button_label(scope: VariantScope) -> str:
    return {
        "all": "用全部变体拓词",
        "best_selling": "用畅销变体拓词",
        "current": "用当前变体拓词",
    }[scope]


SS_BASE = "https://www.sellersprite.com"
MINING_API = SS_BASE + "/v3/api/keyword-miner"
REVERSE_API = SS_BASE + "/v3/api/relation/reversing?market=COM"

# 正向与反查的字段名并不一致（searchRank/searchWeeklyRank vs searchesRank），
# 所以列映射分开定义；列名统一用导出原件的中文表头，ALIASES 才能自动识别。
MINING_COLUMNS: list[tuple[str, str]] = [
    ("关键词", "keyword"),
    ("关键词翻译", "keywordCn"),
    ("月搜索量", "searches"),
    ("ABA月排名", "searchRank"),
    ("ABA周排名", "searchWeeklyRank"),
    ("PPC竞价", "bid"),
    ("购买量", "purchases"),
    ("购买率", "purchaseRate"),
    ("商品数", "products"),
    ("供需比", "supplyDemandRatio"),
    ("平均价格", "avgPrice"),
    ("平均评论数", "avgReviews"),
    ("评分", "avgRating"),
    ("标题密度", "titleDensity"),
    ("SPR", "spr"),
    ("点击集中度", "monopolyClickRate"),
    ("购买转化率", "cvsShareRate"),
    ("点击量", "clicks"),
    ("展示量", "impressions"),
]

REVERSE_COLUMNS: list[tuple[str, str]] = [
    ("关键词", "keywords"),
    ("关键词翻译", "keywordCn"),
    ("月搜索量", "searches"),
    ("ABA周排名", "searchesRank"),
    ("PPC竞价", "bid"),
    ("购买量", "purchases"),
    ("购买率", "purchaseRate"),
    ("商品数", "products"),
    ("流量占比", "trafficPercentage"),
    ("自然排名", "rankPosition.position"),
    ("广告排名", "adPosition.position"),
    ("点击集中度", "top3ClickingRate"),
    ("平均价格", "avgPrice"),
    ("平均评论数", "avgReviews"),
    ("评分", "avgRating"),
    ("标题密度", "titleDensityExact"),
    ("SPR", "cprExact"),
    ("供需比", "supplyDemandRatio"),
    ("近30天广告数", "latest30daysAds"),
]

# 每批并发请求数：卖家精灵对密集请求会限流，4 是实测比较稳的上限。
SS_CONCURRENCY = 4
INVALID_DOWNLOAD_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

SS_FETCH = """async ({url, body}) => {
  const r = await fetch(url, {
    method: 'POST', credentials: 'include',
    headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
    body: JSON.stringify(body)
  });
  return {status: r.status, text: await r.text()};
}"""

# 在浏览器里并发翻页：一次往返拿多页，比 Python 侧串行快一个量级。
SS_FETCH_MANY = """async ({url, bodies}) => {
  const calls = bodies.map(async (body) => {
    const r = await fetch(url, {
      method: 'POST', credentials: 'include',
      headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
      body: JSON.stringify(body)
    });
    return {status: r.status, text: await r.text()};
  });
  return Promise.all(calls);
}"""


def _ss_payload(status: object, text: object) -> dict[str, object]:
    if status != 200 or not isinstance(text, str):
        raise DomainError(
            502, "SELLERSPRITE_API_FAILED", f"卖家精灵接口返回 HTTP {status}，请检查登录状态"
        )
    try:
        payload: object = json.loads(text)
    except ValueError:
        raise DomainError(502, "SELLERSPRITE_API_INVALID", "卖家精灵接口返回非 JSON") from None
    if not isinstance(payload, dict) or payload.get("code") != "OK":
        message = payload.get("message") if isinstance(payload, dict) else ""
        text_message = str(message or "")
        if any(word in text_message for word in ("登录", "登陆")):
            raise DomainError(
                503, "SELLERSPRITE_AUTH_REQUIRED", f"卖家精灵登录已失效：{text_message or '请重新登录'}"
            )
        raise DomainError(
            502, "SELLERSPRITE_API_FAILED", f"卖家精灵接口未返回成功状态：{text_message or '未知'}"
        )
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


SS_LOGIN_URL = SS_BASE + "/w/user/login"
SS_CLIENT_API = SS_BASE + "/v3/api/client"


def _port_open(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0

SS_GET = """async (url) => {
  const r = await fetch(url, {credentials: 'include'});
  return {status: r.status, text: await r.text()};
}"""


def _ss_logged_in(page: "Page") -> bool:
    """/v3/api/client 未登录时 code != OK；已登录返回账号 id。"""
    result: object = page.evaluate(SS_GET, SS_CLIENT_API)
    if not isinstance(result, dict) or result.get("status") != 200:
        return False
    text = result.get("text")
    if not isinstance(text, str):
        return False
    try:
        payload: object = json.loads(text)
    except ValueError:
        return False
    if not isinstance(payload, dict) or payload.get("code") != "OK":
        return False
    data = payload.get("data")
    return isinstance(data, dict) and bool(data.get("id"))


def ensure_sellersprite_login(page: "Page", wait_seconds: float = 240.0) -> None:
    """检查卖家精灵登录态；未登录就跳到登录页，等使用者登录后自动继续。

    与紫鸟 ABA 的登录引导同款逻辑：打开登录页（bring_to_front 让使用者看到），
    每 3 秒探测一次登录态，最长等 4 分钟；超时才报错。
    """
    try:
        if _ss_logged_in(page):
            return
    except Exception:
        pass  # 页面跳转瞬间 evaluate 会失败，直接走登录页兜底
    page.goto(SS_LOGIN_URL, wait_until="domcontentloaded", timeout=45000)
    page.bring_to_front()
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        page.wait_for_timeout(3000)
        try:
            if _ss_logged_in(page):
                return
        except Exception:
            continue  # 登录跳转中 evaluate 抛错，继续等
    raise DomainError(
        503,
        "SELLERSPRITE_AUTH_REQUIRED",
        "等待登录超时，请在紫鸟打开的卖家精灵登录页完成登录后重试",
    )


def _ss_call(page: "Page", url: str, bodies: list[dict[str, object]]) -> list[dict[str, object]]:
    """按批并发调用同一接口，返回每批的 data；批内用浏览器 Promise.all 并行。"""
    collected: list[dict[str, object]] = []
    for start in range(0, len(bodies), SS_CONCURRENCY):
        batch = bodies[start : start + SS_CONCURRENCY]
        result: object = page.evaluate(SS_FETCH_MANY, {"url": url, "bodies": batch})
        if not isinstance(result, list):
            raise DomainError(502, "SELLERSPRITE_API_INVALID", "卖家精灵接口返回结构异常")
        for entry in result:
            if not isinstance(entry, dict):
                raise DomainError(502, "SELLERSPRITE_API_INVALID", "卖家精灵接口返回结构异常")
            collected.append(_ss_payload(entry.get("status"), entry.get("text")))
    return collected


def _ss_collect(
    page: "Page",
    url: str,
    first: dict[str, object],
    body_of,
    concurrency: int = SS_CONCURRENCY,
) -> tuple[list[dict[str, object]], int]:
    """先取第一页确定 total，再并发补齐剩余页；返回条目与供应商声明的总数。"""
    items = [item for item in (first.get("items") or []) if isinstance(item, dict)]
    total = int(first.get("total") or len(items))
    size = len(items) or 1
    if len(items) >= total:
        return items, total
    pages = list(range(2, min((total + size - 1) // size, 60) + 1))
    if pages:
        for chunk in _ss_call(page, url, [body_of(number) for number in pages]):
            items.extend(item for item in (chunk.get("items") or []) if isinstance(item, dict))
    return items, total


def _ss_rows(items: list[dict[str, object]], columns: list[tuple[str, str]], total: int) -> list[list[str]]:
    rows: list[list[str]] = [[label for label, _ in columns]]
    for item in items[:total]:
        rows.append([_extend_value(item, path) for _, path in columns])
    return rows


def collection_filename(request: BrowserCollectionInput) -> str:
    """按业务方向命名生成的卖家精灵 XLSX；多 ASIN 文件名不展开清单。"""
    if request.direction == "reverse":
        if len(request.queries) > 1:
            return "卖家精灵反向多ASIN.xlsx"
        query = request.queries[0]
        return f"卖家精灵反向{INVALID_DOWNLOAD_FILENAME.sub('-', query)}.xlsx"
    query = INVALID_DOWNLOAD_FILENAME.sub("-", request.query) or "核心大词"
    return f"卖家精灵正向{query}.xlsx"


def download_keywords_via_api(
    page: "Page", request: BrowserCollectionInput, target: Path
) -> tuple[str, bytes, int]:
    """正向挖掘与 ASIN 反查都直接调页面接口，跳过页面操作和导出队列。"""
    forward = request.method == "keyword_mining"
    url = MINING_API if forward else REVERSE_API
    body: dict[str, object] = (
        {
            "keyword": request.query,
            "market": 1,
            "pageNum": 1,
            "pageSize": 100,
            "historyDate": "",
            "orderBy": 21,
            "desc": True,
            "filterRootWord": 0,
            "matchType": 0,
            "amazonChoice": False,
            "keywordBidMatchType": "exact",
        }
        if forward
        else {
            "asin": request.query.upper(),
            "limit": 100,
            "skip": 0,
            "month": "",
            "badges": [],
            "conversionKeywordTypes": [],
            "trafficKeywordTypes": [],
            "order": 12,
            "desc": True,
            "exactly": False,
            "ac": False,
            "keywordBidMatchType": "exact",
            "filterDeletedKeywords": False,
        }
    )
    result: object = page.evaluate(SS_FETCH, {"url": url, "body": body})
    if not isinstance(result, dict):
        raise DomainError(502, "SELLERSPRITE_API_INVALID", "卖家精灵接口返回结构异常")
    first = _ss_payload(result.get("status"), result.get("text"))

    if forward:

        def body_of(number: int) -> dict[str, object]:
            return {**body, "pageNum": number}

    else:

        def body_of(number: int) -> dict[str, object]:
            return {**body, "skip": (number - 1) * 100}

    items, total = _ss_collect(page, url, first, body_of)
    if not items:
        raise DomainError(
            422, "SELLERSPRITE_NO_RESULTS", "本次查询未获得关键词，请检查会员权限、站点和最近30天数据"
        )
    columns = MINING_COLUMNS if forward else REVERSE_COLUMNS
    raw = build_xlsx(_ss_rows(items, columns, total), sheet="关键词")
    target.write_bytes(raw)
    return collection_filename(request), raw, total


EXTEND_API = "https://www.sellersprite.com/v3/api/traffic/extend/asin"

# 接口字段与"全部自定义列"一一对应；列名沿用导出原件的中文表头，
# 下游 ALIASES 才能自动映射（关键词 / 月搜索量 / ABA周排名 / PPC竞价 …）。
EXTEND_COLUMNS: list[tuple[str, str]] = [
    ("关键词", "keywords"),
    ("关键词翻译", "keywordCn"),
    ("月搜索量", "searches"),
    ("ABA周排名", "searchesRank"),
    ("PPC竞价", "bid"),
    ("购买量", "purchases"),
    ("购买率", "purchaseRate"),
    ("商品数", "products"),
    ("流量占比", "trafficPercentage"),
    ("自然排名", "rankPosition.position"),
    ("广告排名", "adPosition.position"),
    ("点击集中度", "top3ClickingRate"),
    ("平均价格", "avgPrice"),
    ("平均评论数", "avgReviews"),
    ("评分", "avgRating"),
    ("标题密度", "titleDensityExact"),
    ("SPR", "cprExact"),
    ("供需比", "supplyDemandRatio"),
    ("近30天广告数", "latest30daysAds"),
    ("流量词类型", "trafficKeywordTypes"),
]

# 在页面上下文里发请求，自动带上登录 Cookie，也不触发跨域限制。
EXTEND_FETCH = """async ({url, body}) => {
  const r = await fetch(url, {
    method: 'POST', credentials: 'include',
    headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
    body: JSON.stringify(body)
  });
  return {status: r.status, text: await r.text()};
}"""


def _extend_value(item: dict[str, object], path: str) -> str:
    value: object = item
    for part in path.split("."):
        if not isinstance(value, dict):
            return ""
        value = value.get(part)
    if value is None:
        return ""
    if isinstance(value, list):
        return " / ".join(str(entry) for entry in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _extend_request(page: "Page", request: BrowserCollectionInput, page_no: int) -> dict[str, object]:
    body = {
        "queryVariations": request.variant_scope != "current",
        "asinList": request.queries,
        "originAsinList": request.queries,
        "market": 1,
        "page": page_no,
        "month": "",
        "size": 200,
        "orderColumn": 12,
        "desc": True,
        "exactly": False,
        "ac": False,
        "filterDeletedKeywords": False,
        "keywordBidMatchType": "exact",
    }
    result: object = page.evaluate(EXTEND_FETCH, {"url": EXTEND_API, "body": body})
    if not isinstance(result, dict):
        raise DomainError(502, "SELLERSPRITE_API_INVALID", "卖家精灵接口返回结构异常")
    status = result.get("status")
    text = result.get("text")
    if status != 200 or not isinstance(text, str):
        raise DomainError(
            502, "SELLERSPRITE_API_FAILED", f"卖家精灵接口返回 HTTP {status}，请检查登录状态"
        )
    try:
        payload: object = json.loads(text)
    except ValueError:
        raise DomainError(502, "SELLERSPRITE_API_INVALID", "卖家精灵接口返回非 JSON") from None
    if not isinstance(payload, dict) or payload.get("code") != "OK":
        raise DomainError(502, "SELLERSPRITE_API_FAILED", "卖家精灵接口未返回成功状态")
    inner = payload.get("data")
    if not isinstance(inner, dict):
        raise DomainError(502, "SELLERSPRITE_API_INVALID", "卖家精灵接口缺少数据体")
    return inner


def download_extension_via_api(
    page: "Page", request: BrowserCollectionInput, target: Path
) -> tuple[str, bytes, int]:
    """直接取拓展流量词接口数据，跳过 1~5 分钟的导出队列；分页在浏览器里并发。"""
    if request.variant_scope == "best_selling":
        raise DomainError(
            422,
            "SELLERSPRITE_VARIANT_UNSUPPORTED",
            "接口直取只支持全部/当前变体，畅销变体请在页面手动导出",
        )
    body: dict[str, object] = {
        "queryVariations": request.variant_scope != "current",
        "asinList": request.queries,
        "originAsinList": request.queries,
        "market": 1,
        "page": 1,
        "month": "",
        "size": 200,
        "orderColumn": 12,
        "desc": True,
        "exactly": False,
        "ac": False,
        "filterDeletedKeywords": False,
        "keywordBidMatchType": "exact",
    }
    result: object = page.evaluate(SS_FETCH, {"url": EXTEND_API, "body": body})
    if not isinstance(result, dict):
        raise DomainError(502, "SELLERSPRITE_API_INVALID", "卖家精灵接口返回结构异常")
    first = _ss_payload(result.get("status"), result.get("text"))
    items, total = _ss_collect(page, EXTEND_API, first, lambda number: {**body, "page": number})
    if not items:
        raise DomainError(
            422, "SELLERSPRITE_NO_RESULTS", "本次查询未获得流量词，请检查站点与最近30天数据"
        )
    raw = build_xlsx(_ss_rows(items, EXTEND_COLUMNS, total), sheet="流量词")
    target.write_bytes(raw)
    return collection_filename(request), raw, total


def click_variant_button(page: "Page", scope: VariantScope) -> int:
    """拓展流量词页面的按钮文案带数量，如「用全部变体拓词 (342)」。

    因此不能用 exact 匹配；点它并返回括号里的数量作为本次结果数。
    按钮只在查询完成后出现，这里自己等到 45 秒，避免和计数循环抢时序。
    """
    pattern = re.compile(rf"^{re.escape(variant_button_label(scope))}\s*\(([\d,]+)\)")
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        page.wait_for_timeout(1000)
        candidates = page.get_by_role("button", name=pattern)
        if not candidates.count():
            continue
        button = candidates.first
        match = pattern.search((button.inner_text() or "").replace("\n", " "))
        if match is None:
            continue
        count = int(match[1].replace(",", ""))
        if count <= 0:
            return 0
        button.click(timeout=15000)
        return count
    return 0


def ensure_chinese(page: "Page", diagnostic_dir: Path | None = None) -> None:
    """控件文案随界面语言变化；非中文时自动切到中文，失败则明确报错。"""
    from playwright.sync_api import Error as PlaywrightError

    def language() -> str:
        return (page.locator("html").get_attribute("lang") or "").lower()

    def chinese_interface() -> bool:
        """以可见中文文本量判断，不依赖某个按钮的具体文案。

        SPA 的 html lang 常是默认的 en，而 v3 页面按钮文案也与旧页不同；
        旧规则（必须同时出现“立即查询”和“最近30天”）会把中文页误判成英文。
        语言菜单里的“简体中文”在隐藏层时不计入 inner_text，不会造成误判。
        """
        try:
            text = page.locator("body").inner_text()
        except PlaywrightError:
            return False
        return len(CJK.findall(text)) >= CJK_THRESHOLD

    # 页面是 SPA：domcontentloaded 时头部和语言状态可能还没渲染。
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if language().startswith("zh") or chinese_interface():
            return
        if page.locator(", ".join(LANGUAGE_TRIGGERS)).count():
            break
        page.wait_for_timeout(500)
    if language().startswith("zh") or chinese_interface():
        return
    # 登录前后是两套语言菜单：登录后为 .language-trigger + menuitemradio，
    # 未登录为 .nation / .el-dropdown-selfdefine + li.lang-item；
    # v3 页面还可能用普通下拉菜单，因此在可见弹层内按文案兜底。
    opened = False
    for selector in LANGUAGE_TRIGGERS:
        trigger = page.locator(selector).first
        if not trigger.count():
            continue
        try:
            trigger.click(timeout=4000)
            opened = True
            break
        except PlaywrightError:
            continue
    if opened:
        scope = page
        try:
            popup = page.locator(", ".join(LANGUAGE_MENU_SCOPES)).last
            if popup.count():
                scope = popup
        except PlaywrightError:
            pass
        clicked = False
        for selector in LANGUAGE_ITEMS:
            item = scope.locator(selector).filter(has_text=CHINESE_OPTION).first
            try:
                if not item.count():
                    continue
                item.click(timeout=4000)
                clicked = True
                break
            except PlaywrightError:
                continue
        if not clicked:
            # 弹层结构未知时，退回到整页找“中文/简体中文”项。
            with contextlib.suppress(PlaywrightError):
                page.get_by_text(CHINESE_OPTION, exact=False).first.click(timeout=4000)
        page.wait_for_timeout(2500)
    for _ in range(20):
        if language().startswith("zh") or chinese_interface():
            return
        page.wait_for_timeout(500)
    capture_page_diagnostic(page, diagnostic_dir, "language")
    # 登录页常是英文、也没有语言菜单；此时报“语言不支持”会误导排查。
    try:
        text = page.locator("body").inner_text()
    except PlaywrightError:
        text = ""
    if LOGIN_HINT.search(text) or "/login" in (getattr(page, "url", "") or "").lower():
        raise DomainError(
            503,
            "SELLERSPRITE_LOGIN_REQUIRED",
            "卖家精灵登录已失效，请在紫鸟中重新登录后重试",
        )
    raise DomainError(
        422,
        "SELLERSPRITE_LANGUAGE_UNSUPPORTED",
        "卖家精灵页面不是中文，自动切换失败；请手动切换页面语言后重试",
    )


def capture_page_diagnostic(page: "Page", directory: Path | None, label: str) -> None:
    """页面操作失败时留一份现场（截图 + 可见正文），便于确认是改版还是登录失效。"""
    if directory is None:
        return
    try:
        directory.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        page.screenshot(path=str(directory / f"{label}-{stamp}.png"), full_page=False)
        detail = (
            f"url={page.url}\n"
            f"html-lang={page.locator('html').get_attribute('lang')}\n\n"
            f"{page.locator('body').inner_text()}"
        )
        (directory / f"{label}-{stamp}.txt").write_text(detail, encoding="utf-8")
    except Exception:  # noqa: BLE001 - 诊断失败不能覆盖原始错误
        return


def find_select_with_value(page: "Page", values: tuple[str, ...]) -> "Locator | None":
    """按当前显示值定位可见下拉，避免页面改版后写死的第 1/第 2 个下拉失效。"""
    from playwright.sync_api import Error as PlaywrightError

    selects = page.locator(".el-select:visible")
    for index in range(selects.count()):
        select = selects.nth(index)
        try:
            current = select.locator("input").input_value().strip()
        except PlaywrightError:
            continue
        if current in values:
            return select
    return None


def fill_traffic_extension_asins(asin_input: "Locator", queries: list[str]) -> None:
    """按卖家精灵页面约定，将同批 ASIN 用空格一次性写入并读回核验。"""
    value = " ".join(queries)
    asin_input.fill(value)
    entered = asin_input.input_value().split()
    if [item.upper() for item in entered] != [item.upper() for item in queries]:
        raise DomainError(
            502,
            "SELLERSPRITE_ASIN_INPUT_MISMATCH",
            "卖家精灵未完整接收本批 ASIN，请重试",
        )


def select_all_custom_columns(
    page: "Page", diagnostic_dir: Path | None = None, label: str = "custom-columns"
) -> None:
    """导出前启用卖家精灵提供的全部自定义指标列。"""
    from playwright.sync_api import Error as PlaywrightError

    try:
        # 当前页面按钮的可见文字是“ 自定义列”，但组件未暴露可匹配的 accessible name；
        # 因此按真实可见按钮文本定位，避免 get_by_role(name=...) 稳定返回 0。
        opener = page.locator("button:visible").filter(has_text=CUSTOM_COLUMNS).first
        # 结果数是先渲染的，工具栏会稍后再出现；连续查询时立即查找会把“还在渲染”
        # 误判成“页面没有自定义列”，所以给工具栏留渲染时间。
        deadline = time.monotonic() + 20
        while not opener.count() and time.monotonic() < deadline:
            page.wait_for_timeout(1000)
        if not opener.count():
            capture_page_diagnostic(page, diagnostic_dir, label)
            raise DomainError(
                502,
                "SELLERSPRITE_CUSTOM_COLUMNS_MISSING",
                "未找到卖家精灵“自定义列”，未执行导出",
            )
        opener.click(timeout=15000)
        # 点击列设置后页面只打开一个可见弹层；不能用锚定标题正则过滤整个弹层文本，
        # 因为弹层正文还包含“全选”、字段列表、保存和重置。
        dialog = page.locator('[role="dialog"]:visible, .el-dialog:visible').last
        dialog.wait_for(timeout=15000)
        # Element UI 的原生 input 被样式移到视口外，check(force=True) 会报
        # "Element is outside of the viewport"；只能点可见的 label，勾选状态仍读 input。
        labels = dialog.locator("label.el-checkbox")
        if not labels.count():
            raise DomainError(
                502,
                "SELLERSPRITE_CUSTOM_COLUMNS_EMPTY",
                "卖家精灵“自定义列”中没有可选字段，未执行导出",
            )

        def checked(index: int) -> bool:
            return labels.nth(index).locator('input[type="checkbox"]').is_checked()

        # 先勾“全选”，再逐项补齐并核验，兼容全选框处于半选状态的页面。
        if not checked(0):
            labels.nth(0).click(timeout=15000)
        page.wait_for_timeout(250)
        for index in range(labels.count()):
            if not checked(index):
                labels.nth(index).click(timeout=15000)
        page.wait_for_timeout(250)
        if any(not checked(index) for index in range(labels.count())):
            raise DomainError(
                502,
                "SELLERSPRITE_CUSTOM_COLUMNS_INCOMPLETE",
                "卖家精灵自定义列未能全部选中，未执行导出",
            )
        dialog.locator("button:visible").filter(has_text=SAVE_COLUMNS).first.click(timeout=15000)
        dialog.wait_for(state="hidden", timeout=15000)
    except PlaywrightError:
        raise DomainError(
            502,
            "SELLERSPRITE_CUSTOM_COLUMNS_FAILED",
            "卖家精灵自定义列设置失败，未执行导出",
        ) from None


def export_filename_matches(filename: str, query: str, direction: str) -> bool:
    if not filename.casefold().endswith(".xlsx"):
        return False
    token = re.sub(r"[^A-Za-z0-9]+", "-", query.strip()).strip("-").casefold()
    prefix = "keywordmining-us-" if direction == "forward" else "reverseasin-us-"
    return filename.casefold().startswith(prefix + token + "-")


def recent_export_matches(filename: str) -> bool:
    """文件名必须含供应商最近30天标记，不能误收同查询的月导出。"""
    return bool(
        re.search(r"(?:^|-)(?:Last-30-days|最近30天)(?:-|\.xlsx$)", filename, re.IGNORECASE)
    )


def export_filename_matches_request(filename: str, request: BrowserCollectionInput) -> bool:
    if request.method != "traffic_extension":
        return export_filename_matches(filename, request.query, request.direction)
    match = TRAFFIC_EXPORT_NAME.fullmatch(filename)
    if match is None or not request.queries:
        return False
    # 文件名只保留首个 ASIN，用批量条数锁定“就是这一次的 N 个 ASIN”导出。
    return int(match["count"]) == len(request.queries) and (
        match["asin"].upper() == request.queries[0].upper()
    )


class SellerspriteDownloader(Protocol):
    """卖家精灵浏览器导出的最小接口；本地实现与远程 Agent 客户端都实现它。"""

    def download(self, request: BrowserCollectionInput, target: Path) -> tuple[str, bytes, int]: ...


class SellerspriteBrowserDownloader:
    def __init__(self, port: int | None = None, *, directory: Path | None = None) -> None:
        self.port = port
        self.directory = directory or Path(__file__).resolve().parents[2] / "data" / "sellersprite"

    def download(self, request: BrowserCollectionInput, target: Path) -> tuple[str, bytes, int]:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright

        if request.period != "last_30_days":
            raise DomainError(422, "SELLERSPRITE_PERIOD_INVALID", "当前采集使用最近30天")
        method = request.method
        if method is None:
            raise DomainError(422, "SELLERSPRITE_METHOD_INVALID", "缺少卖家精灵采集方式")
        # 优先紫鸟浏览器会话（与 ABA 同源）；紫鸟没开时回落日常 Chrome。
        try:
            port = self.port or ensure_port(
                self.directory, "KEYWORD_SELLERSPRITE_CDP_PORT", shared=self.directory.parent / "aba"
            )
        except DomainError:
            if self.port is not None or not _port_open(9222):
                raise
            port = 9222
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.connect_over_cdp(
                    # 复用同机紫鸟登录态；没有远程采集代理时，服务器无法操作本地电脑店铺。
                    f"http://127.0.0.1:{port}",
                    timeout=10000,
                )
                # 优先找已打开卖家精灵页的 context（多开时锁定登录了的那个）；
                # 接口直取只需登录 Cookie，没有开页时直接用唯一 context 兜底。
                contexts = [
                    c
                    for c in browser.contexts
                    if any(
                        urlsplit(p.url).hostname in {"www.sellersprite.com", "sellersprite.com"}
                        for p in c.pages
                    )
                ] or list(browser.contexts)
                if len(contexts) != 1:
                    raise DomainError(
                        503,
                        "SELLERSPRITE_CDP_REQUIRED",
                        "请在紫鸟中登录卖家精灵，保持唯一账号会话",
                    )
                context = contexts[0]
                page = context.new_page()
                # 三种采集方式都直接调页面接口：省掉页面点击、自定义列和 1~5 分钟的导出队列。
                try:
                    page.goto(
                        SS_BASE + "/v3/keyword-reverse",
                        wait_until="domcontentloaded",
                        timeout=45000,
                    )
                    # welcome 会按登录态再跳一次；跳转期间 evaluate 会报
                    # "Execution context was destroyed"，先等它稳定。
                    page.wait_for_timeout(1500)

                    def _collect() -> tuple[str, bytes, int]:
                        if method == "traffic_extension":
                            return download_extension_via_api(page, request, target)
                        return download_keywords_via_api(page, request, target)

                    # 未登录就跳登录页等使用者登录，登录后自动继续。
                    ensure_sellersprite_login(page)
                    try:
                        return _collect()
                    except DomainError as error:
                        if error.code != "SELLERSPRITE_AUTH_REQUIRED":
                            raise
                        # 采集中途掉登录态：再引导登录一次后重试。
                        ensure_sellersprite_login(page)
                        return _collect()
                finally:
                    page.close()

        except PlaywrightError:
            # 设置 KEYWORD_DEBUG_BROWSER=1 时输出原始堆栈，便于定位页面改版后的选择器问题。
            if os.environ.get("KEYWORD_DEBUG_BROWSER"):
                import traceback

                traceback.print_exc()
            raise DomainError(
                503,
                "SELLERSPRITE_BROWSER_UNAVAILABLE",
                "卖家精灵浏览器连接、页面操作或下载失败，请检查紫鸟店铺、登录及页面语言",
            ) from None


    def download_original(self, request: BrowserCollectionInput, target: Path) -> tuple[str, bytes, int]:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright

        if request.period != "last_30_days":
            raise DomainError(422, "SELLERSPRITE_PERIOD_INVALID", "当前采集使用最近30天")
        method = request.method
        if method is None:
            raise DomainError(422, "SELLERSPRITE_METHOD_INVALID", "缺少卖家精灵采集方式")
        query = request.query
        port = self.port or ensure_port(
            self.directory, "KEYWORD_SELLERSPRITE_CDP_PORT", shared=self.directory.parent / "aba"
        )
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.connect_over_cdp(
                    # 复用同机紫鸟登录态；没有远程采集代理时，服务器无法操作本地电脑店铺。
                    f"http://127.0.0.1:{port}",
                    timeout=10000,
                )
                contexts = [
                    c
                    for c in browser.contexts
                    if any(
                        urlsplit(p.url).hostname in {"www.sellersprite.com", "sellersprite.com"}
                        for p in c.pages
                    )
                ]
                if len(contexts) != 1:
                    raise DomainError(
                        503,
                        "SELLERSPRITE_CDP_REQUIRED",
                        "请在紫鸟中登录卖家精灵，保持唯一账号会话",
                    )
                context = contexts[0]
                page = context.new_page()
                logs = context.new_page()
                try:
                    # 只认本次新任务；不下载账号中其他人的历史导出。
                    baseline: set[str] = set()
                    if method in {"keyword_mining", "traffic_extension"}:
                        logs.goto(
                            "https://www.sellersprite.com/v2/export-log",
                            wait_until="domcontentloaded",
                            timeout=45000,
                        )
                        logs.locator("table tbody tr").first.wait_for(timeout=20000)
                        baseline = set(
                            logs.locator(".remark-edit-btn[data-id]").evaluate_all(
                                '(els)=>els.map(e=>e.getAttribute("data-id"))'
                            )
                        )
                    page.goto(
                        "https://www.sellersprite.com" + collection_path(method),
                        wait_until="domcontentloaded",
                        timeout=45000,
                    )
                    ensure_chinese(page, self.directory / "diagnostics")
                    if method == "traffic_extension":
                        asin_input = page.locator(
                            'input[placeholder*="ASIN"]:visible, '
                            'textarea[placeholder*="ASIN"]:visible, '
                            ".el-select__input:visible"
                        ).first
                        asin_input.wait_for(timeout=30000)
                        fill_traffic_extension_asins(asin_input, request.queries)
                    else:
                        placeholder = (
                            KEYWORD_PLACEHOLDER if method == "keyword_mining" else ASIN_PLACEHOLDER
                        )
                        page.get_by_placeholder(placeholder).fill(query, timeout=30000)
                    marketplace_select = (
                        find_select_with_value(page, tuple(MARKETPLACE_LABELS))
                        or page.locator(".el-select:visible").first
                    )
                    marketplace = marketplace_select.locator("input").input_value()
                    if marketplace not in US_MARKETPLACES:
                        raise DomainError(
                            422,
                            "SELLERSPRITE_MARKETPLACE_MISMATCH",
                            "请将卖家精灵查询站点设为美国站",
                        )
                    period_select = find_select_with_value(page, PERIOD_LABELS) or page.locator(
                        ".el-select:visible"
                    ).nth(1)
                    period_select.click()
                    page.locator(".el-select-dropdown:visible").get_by_text(
                        PERIOD_OPTION
                    ).first.click()
                    # 结果页会跳到新路由且页面上不再显示周期选项，必须在查询前确认。
                    page.wait_for_timeout(300)
                    if not PERIOD_OPTION.search(period_select.locator("input").input_value()):
                        raise DomainError(
                            422,
                            "SELLERSPRITE_PERIOD_CHANGED",
                            "无法把卖家精灵查询周期设为最近30天，请手动调整后重试",
                        )
                    page.get_by_role("button", name=QUERY_BUTTON).click(timeout=15000)
                    if method == "traffic_extension":
                        if request.variant_scope is None:
                            raise DomainError(
                                422,
                                "SELLERSPRITE_VARIANT_SCOPE_REQUIRED",
                                "拓展流量词须选择变体范围",
                            )
                        # “选择拓词方式”弹窗的按钮带结果数，如“用全部变体拓词 (1,128)”，
                        # 只能按文案包含匹配；精确匹配会一直等不到按钮。
                        page.get_by_role(
                            "button", name=variant_button_label(request.variant_scope)
                        ).first.click(timeout=20000)
                    count = 0
                    deadline = time.monotonic() + 35
                    while time.monotonic() < deadline:
                        page.wait_for_timeout(1000)
                        match = RESULT_COUNT.search(page.locator("body").inner_text())
                        if match and int(match[1].replace(",", "")) > 0:
                            count = int(match[1].replace(",", ""))
                            break
                    if not count:
                        raise DomainError(
                            422,
                            "SELLERSPRITE_NO_RESULTS",
                            "本次查询未获得关键词，请检查会员权限、站点和最近30天数据",
                        )
                    select_all_custom_columns(
                        page, self.directory / "diagnostics", f"custom-columns-{method}"
                    )
                    button = page.locator("button").filter(has_text=EXPORT_BUTTON)
                    if method == "keyword_reverse":
                        with page.expect_download(timeout=60000) as pending:
                            button.click(timeout=15000)
                        download = pending.value
                        filename = download.suggested_filename
                        if not export_filename_matches_request(
                            filename, request
                        ) or not recent_export_matches(filename):
                            raise DomainError(
                                502,
                                "SELLERSPRITE_EXPORT_MISMATCH",
                                "反查原件与本次查询或最近30天窗口不一致",
                            )
                        download.save_as(target)
                        if download.failure():
                            raise DomainError(
                                502, "SELLERSPRITE_DOWNLOAD_FAILED", "反查原件下载失败"
                            )
                    else:
                        # 关键词挖掘和拓展流量词都是异步导出：点“导出”只弹“数据导出中”，
                        # 文件生成后要到“我的导出”里下载，不会触发浏览器下载事件。
                        prefix = (
                            KEYWORD_MINER_PREFIX
                            if method == "keyword_mining"
                            else TRAFFIC_EXPORT_PREFIX
                        )
                        button.click(timeout=15000)
                        page.locator("[role=dialog]:visible").get_by_text(
                            VIEW_EXPORT
                        ).first.wait_for(timeout=15000)
                        job_id: str | None = None
                        filename = ""
                        deadline = time.monotonic() + 360
                        while time.monotonic() < deadline:
                            logs.reload(wait_until="domcontentloaded", timeout=45000)
                            logs.locator("table tbody tr").first.wait_for(timeout=20000)
                            candidates: list[tuple[str, Locator, str]] = []
                            rows = logs.locator("tr").filter(has_text=prefix)
                            for index in range(rows.count()):
                                row = rows.nth(index)
                                identifier = row.locator(".remark-edit-btn[data-id]").get_attribute(
                                    "data-id"
                                )
                                text = row.inner_text()
                                name = re.search(re.escape(prefix) + r"[^\s]+", text)
                                if not name or not identifier or identifier in baseline:
                                    continue
                                if (
                                    export_filename_matches_request(name[0] + ".xlsx", request)
                                    # 拓展流量词导出名不带 30 天标记，周期已在查询前核对。
                                    and (
                                        method != "keyword_mining"
                                        or recent_export_matches(name[0] + ".xlsx")
                                    )
                                    and (job_id is None or identifier == job_id)
                                ):
                                    candidates.append((identifier, row, name[0] + ".xlsx"))
                            if len(candidates) > 1:
                                raise DomainError(
                                    409,
                                    "SELLERSPRITE_EXPORT_AMBIGUOUS",
                                    "同时发现多份同条件新导出，请在我的导出页面核对后手动导入",
                                )
                            if candidates:
                                job_id, row, filename = candidates[0]
                                text = row.inner_text()
                                if "导出失败" in text:
                                    raise DomainError(
                                        502, "SELLERSPRITE_EXPORT_FAILED", "卖家精灵报告生成失败"
                                    )
                                if "已完成" in text:
                                    with logs.expect_download(timeout=60000) as pending:
                                        row.locator("a.download-excel").click()
                                    download = pending.value
                                    if download.suggested_filename != filename:
                                        raise DomainError(
                                            502,
                                            "SELLERSPRITE_EXPORT_MISMATCH",
                                            "下载原件名称与本次导出任务不一致",
                                        )
                                    download.save_as(target)
                                    if download.failure():
                                        raise DomainError(
                                            502, "SELLERSPRITE_DOWNLOAD_FAILED", "卖家精灵原件下载失败"
                                        )
                                    break
                            time.sleep(5)
                        else:
                            raise DomainError(
                                504,
                                "SELLERSPRITE_EXPORT_TIMEOUT",
                                "卖家精灵报告生成超过6分钟，请检查我的导出",
                            )
                    if not target.is_file() or not 0 < target.stat().st_size <= MAX_BYTES:
                        raise DomainError(422, "SELLERSPRITE_FILE_SIZE", "下载原件为空或超过10 MiB")
                    return filename, target.read_bytes(), count
                except DomainError:
                    raise
                except Exception:
                    # 页面仍在时先留现场；意外异常也要能复盘，不能只剩一句通用提示。
                    capture_page_diagnostic(
                        page, self.directory / "diagnostics", f"browser-{method}"
                    )
                    raise
                finally:
                    page.close()
                    logs.close()
        except PlaywrightError:
            raise DomainError(
                503,
                "SELLERSPRITE_BROWSER_UNAVAILABLE",
                "卖家精灵浏览器连接、页面操作或下载失败，请检查紫鸟店铺、登录及页面语言",
            ) from None
