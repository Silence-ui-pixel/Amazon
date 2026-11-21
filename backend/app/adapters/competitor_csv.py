"""读取商机探测器商品导出；不猜测缺失币种或价格，不丢弃错误行。"""

import csv
import io
import re
from decimal import Decimal, InvalidOperation

from pydantic import ValidationError

from app.domain.competitors import Competitor, CompetitorSource
from app.domain.errors import DomainError

ALIASES = {
    "asin": {"asin", "商品asin"},
    "title": {"productname", "producttitle", "title", "商品名称", "商品标题", "商品"},
    "average": {
        "averagesellingprice90days",
        "averagesellingpricet90",
        "平均售价90天",
        "averagesellingpricepast90days",
        "平均销售价格90天",
    },
    "price": {"price", "售价", "价格", "平均售价", "averagesellingprice", "平均销售价格"},
    "currency": {"currency", "货币", "币种"},
    "image": {"imageurl", "asinimageurl", "image", "图片链接", "主图链接", "商品图片"},
}


def normalize_header(value: str) -> str:
    return re.sub(r"[\s_()（）\-]", "", value).casefold().lstrip("\ufeff")


def parse_competitor_csv(
    text: str, label: str, *, require_30_day: bool = False
) -> list[Competitor]:
    try:
        # Amazon 原件在表头前有市场名称和更新时间；仅跳过已识别的说明区。
        lines = text.lstrip("\ufeff").splitlines(keepends=True)
        if lines and lines[0].startswith((
            "细分市场名称:", "Niche Name:", "Niche name:", "买家搜索",
            "Products customers clicked", "Products clicked",
        )):
            header = next(
                (
                    i
                    for i, line in enumerate(lines[:12])
                    if re.search(r'(?:^|[,\t;])"?ASIN"?(?:[,\t;]|$)', line)
                ),
                None,
            )
            if header is None:
                raise ValueError("缺少商品表头")
            text = "".join(lines[header:])
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",\t;")
        # Amazon 用双引号转义英寸符号；样本内没有引号时 Sniffer 会误判。
        reader = csv.DictReader(
            io.StringIO(text.lstrip("\ufeff")), dialect=dialect, doublequote=True
        )
        headers = reader.fieldnames or []
        columns: dict[str, str] = {}
        for key, aliases in ALIASES.items():
            matches = [header for header in headers if normalize_header(header) in aliases]
            if len(matches) > 1:
                raise ValueError("重复字段")
            if matches:
                columns[key] = matches[0]
        def period_columns(days: int) -> list[str]:
            return [
                header
                for header in headers
                if re.fullmatch(
                    rf"(?:平均售价(?:过去|最近){days}天(?:内)?|averagesellingprice(?:past|last)?{days}days)",
                    normalize_header(re.sub(r"[(（][A-Z]{3}[)）]", "", header, flags=re.IGNORECASE)),
                    re.IGNORECASE,
                )
            ]

        average30 = period_columns(30)
        average360 = period_columns(360)
        if len(average30) > 1:
            raise ValueError("重复的 30 天均价字段")
        if len(average360) > 1:
            raise ValueError("重复的 360 天均价字段")
        header_currency = ""
        if average30:
            columns["average30"] = average30[0]
        if average360:
            columns["average360"] = average360[0]
        price_header = average30[0] if average30 else average360[0] if average360 else ""
        if price_header:
            currencies = set(re.findall(r"[(（]([A-Z]{3})[)）]", price_header, re.IGNORECASE))
            if len(currencies) > 1:
                raise ValueError("均价表头存在冲突币种")
            header_currency = next(iter(currencies), "").upper()
        if require_30_day and not average30:
            raise DomainError(
                422,
                "COMPETITOR_CSV_30_DAY_REQUIRED",
                "自动筛选竞对必须使用商机探测器的 30 天平均售价列",
            )
        if "asin" not in columns:
            raise ValueError("缺少 ASIN 列")
        results: list[Competitor] = []
        seen: set[str] = set()
        for number, row in enumerate(reader, 2):
            if number > 502 or None in row:
                raise ValueError("最多 500 条商品，且每行字段数须与表头一致")

            cells = {key: (row.get(column) or "").strip() for key, column in columns.items()}

            def cell(key: str, cells: dict[str, str] = cells) -> str:
                return cells.get(key, "")

            asin = cell("asin").upper()
            if not asin and not any(row.values()):
                continue
            if asin in seen:
                raise ValueError(f"第 {number} 行 ASIN 重复，请先核对重复商品")
            seen.add(asin)
            if header_currency and cell("currency") and cell("currency").upper() != header_currency:
                raise ValueError(f"第 {number} 行币种与均价表头冲突")
            # 选定整列口径；30 天值缺失时保留缺失，不能逐行借用其他时间段。
            price_key = next((key for key in ("average30", "average360", "average", "price") if key in columns), "price")
            raw_price = cell(price_key)
            price: Decimal | None = None
            if raw_price and raw_price.casefold() not in ("-", "n/a", "--", "未知"):
                token = re.match(r"^(USD|EUR|GBP|CAD|JPY|\$|€|£|¥)", raw_price)
                currency = cell("currency").upper()
                if token and currency:
                    marker = token[1]
                    supported = (
                        {"USD", "CAD", "AUD", "NZD", "SGD", "HKD"}
                        if marker == "$"
                        else {"JPY", "CNY"}
                        if marker == "¥"
                        else {{"€": "EUR", "£": "GBP"}.get(marker, marker)}
                    )
                    if currency not in supported:
                        raise ValueError(f"第 {number} 行价格标记与币种列冲突，请核对币种")
                # 仅接受明确小数格式；本地化逗号小数需要用户先统一导出格式。
                raw_price = re.sub(r"^(USD|EUR|GBP|CAD|JPY|\$|€|£|¥)\s*", "", raw_price)
                if not re.fullmatch(r"\d+(\.\d{1,4})?", raw_price):
                    raise ValueError(f"第 {number} 行价格格式不明确，请使用 23.50 格式")
                price = Decimal(raw_price)
            results.append(
                Competitor(
                    asin=asin,
                    title=cell("title")[:2000],
                    image_url=cell("image"),
                    price=price,
                    currency=cell("currency").upper() or header_currency or None,
                    price_basis="average_30d"
                    if price_key == "average30"
                    else "average_360d"
                    if price_key == "average360"
                    else "average_90d"
                    if price_key == "average"
                    else "export"
                    if price
                    else "unknown",
                    sources=[CompetitorSource(kind="poe_csv", label=label)],
                )
            )
        if not results or len(results) > 500:
            raise ValueError("请导入 1 至 500 条商品")
        return results
    except (csv.Error, ValueError, InvalidOperation, ValidationError) as error:
        raise DomainError(422, "COMPETITOR_CSV_INVALID", f"商品 CSV 无法导入：{error}") from None
