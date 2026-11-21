"""第四步 ABA 正向单核心词 XLSX；保留原始来源行并插入确定性指标。"""

import re
from datetime import date
from decimal import Decimal
from typing import Literal

from app.adapters.aba import normalize_row
from app.adapters.xlsx_simple import build_xlsx
from app.domain.aba import AbaQueryReport, AbaRow
from app.domain.errors import DomainError
from app.domain.traffic import Metrics, RuleSet

INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
INVALID_SHEET = re.compile(r"[\[\]:*?/\\\x00-\x1f]")
FILENAME_SUFFIX = ".xlsx"
MAX_FILENAME_UTF16_UNITS = 255
WorkbookDirection = Literal["forward", "reverse"]

KNOWN_COLUMNS = [
    *(f"st-top-brand-{index}" for index in range(1, 4)),
    *(f"st-top-category-{index}" for index in range(1, 4)),
    *(f"st-top-asin-{index}" for index in range(1, 4)),
    *(f"st-top-asin-click-share-{index}" for index in range(1, 4)),
    *(f"st-top-asin-conversion-share-{index}" for index in range(1, 4)),
]
KNOWN_HEADERS = {
    **{f"st-top-brand-{index}": f"点击量第 {index} 的品牌" for index in range(1, 4)},
    **{f"st-top-category-{index}": f"点击量第 {index} 的分类" for index in range(1, 4)},
    **{f"st-top-asin-{index}": f"点击量第 {index} 商品 ASIN" for index in range(1, 4)},
    **{
        f"st-top-asin-click-share-{index}": f"第 {index} 商品点击份额"
        for index in range(1, 4)
    },
    **{
        f"st-top-asin-conversion-share-{index}": f"第 {index} 商品转化份额"
        for index in range(1, 4)
    },
}
SOURCE_KEYS = {"st-search-frequency", "st-search-term"}


def _safe_text(value: object) -> str:
    return "" if value is None else str(value)


def _percent(value: Decimal | None) -> str:
    return "" if value is None else f"{value * Decimal('100'):.2f}%"


def _utf16_units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _truncate_utf16(value: str, max_units: int) -> str:
    output: list[str] = []
    used = 0
    for char in value:
        units = 2 if ord(char) > 0xFFFF else 1
        if used + units > max_units:
            break
        output.append(char)
        used += units
    return "".join(output)


def _filename_digest(value: str) -> str:
    digest = 0xCBF29CE484222325
    for byte in value.encode("utf-8"):
        digest ^= byte
        digest = digest * 0x100000001B3 & 0xFFFFFFFFFFFFFFFF
    return f"{digest:016x}"


def query_filename(query: str, *, direction: WorkbookDirection = "forward") -> str:
    """生成 Windows、浏览器和 Content-Disposition 都可用的中文文件名。"""
    if direction == "reverse":
        count = len([asin for asin in query.split(",") if asin.strip()])
        return f"第四步ABA反向{count}个ASIN{FILENAME_SUFFIX}"
    prefix = "第四步ABA正向"
    cleaned = INVALID_FILENAME.sub("-", query)
    cleaned = cleaned or "核心大词"
    filename = f"{prefix}{cleaned}{FILENAME_SUFFIX}"
    if _utf16_units(filename) <= MAX_FILENAME_UTF16_UNITS:
        return filename
    marker = f"~{_filename_digest(query)}"
    available = MAX_FILENAME_UTF16_UNITS - _utf16_units(prefix + marker + FILENAME_SUFFIX)
    return f"{prefix}{_truncate_utf16(cleaned, available)}{marker}{FILENAME_SUFFIX}"


def _sheet_name(query: str, direction: WorkbookDirection) -> str:
    if direction == "reverse":
        count = len([asin for asin in query.split(",") if asin.strip()])
        return f"ABA反向-{count}个ASIN"
    cleaned = INVALID_SHEET.sub("-", query)
    name = (f"ABA正向-{cleaned or '核心大词'}")[:31].strip("'")
    return name or "ABA正向-核心大词"


def _metrics(row: AbaRow, raw: dict[str, object]) -> Metrics:
    rules = RuleSet()
    if row.traffic_metrics is not None and row.traffic_metrics.rule_id == rules.id:
        return row.traffic_metrics.metrics.model_copy(deep=True)
    try:
        normalized = normalize_row(raw)
    except DomainError:
        return Metrics(missing=["原始 ABA 行无法计算指标"])
    if (
        normalized.keyword.casefold() != row.keyword.casefold()
        or normalized.search_frequency_rank != row.search_frequency_rank
        or normalized.traffic_metrics is None
    ):
        return Metrics(missing=["原始 ABA 行与解析结果不一致"])
    return normalized.traffic_metrics.metrics.model_copy(deep=True)


def build_query_workbook(
    report: AbaQueryReport,
    week_start: date,
    week_end: date,
    *,
    direction: WorkbookDirection = "forward",
) -> bytes:
    """把一次正向核心词或反向联合 ASIN 查询生成独立单表 XLSX。"""
    extras: list[str] = []
    for raw in report.raw_rows:
        for key in raw:
            if key not in SOURCE_KEYS and key not in KNOWN_HEADERS and key not in extras:
                extras.append(key)
    raw_columns = [key for key in KNOWN_COLUMNS if any(key in row for row in report.raw_rows)]
    raw_columns.extend(extras)
    rows = [
        [
            "报告范围=每周",
            f"起止日期={week_start.isoformat()} 至 {week_end.isoformat()}",
            f"{'查询核心大词' if direction == 'forward' else '查询竞对ASIN'}={report.query}",
            f"采集状态={report.stop_reason}",
        ],
        [
            "搜索频率排名",
            "搜索词",
            "购买能力（>60%）",
            "垄断程度（TOP3 累计>35%）",
            *[KNOWN_HEADERS.get(key, key) for key in raw_columns],
            "指标缺失原因",
        ],
    ]
    for index, row in enumerate(report.rows):
        raw = report.raw_rows[index] if index < len(report.raw_rows) else {}
        metrics = _metrics(row, raw)
        rows.append(
            [
                str(row.search_frequency_rank),
                row.keyword,
                _percent(metrics.purchase_ratio),
                _percent(metrics.conversion_sum3),
                *[_safe_text(raw.get(key)) for key in raw_columns],
                "；".join(metrics.missing),
            ]
        )
    return build_xlsx(rows, sheet=_sheet_name(report.query, direction))
