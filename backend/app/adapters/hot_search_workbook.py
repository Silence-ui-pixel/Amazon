"""把热门搜索词官方结构化行导出为多商品词 XLSX。"""

import io
import re
from decimal import Decimal
from xml.etree.ElementTree import Element, SubElement, tostring
from zipfile import ZIP_DEFLATED, ZipFile

from app.domain.aba import AbaProduct, AbaQueryReport
from app.domain.hot_search_collection import HotSearchCollectionJob
from app.domain.traffic import MetricInput, RuleSet, evaluate

INVALID_SHEET = re.compile(r"[\[\]:*?/\\]")

LEADING_HEADERS = [
    "搜索频率排名",
    "搜索词",
    "购买能力",
    "垄断程度（TOP3 累计）",
    "点击量第 1 的品牌",
    "点击量第 2 的品牌",
    "点击量第 3 的品牌",
    "点击量最高的分类",
    "点击量第 2 的分类",
    "点击量第 3 的分类",
    "点击量第 1 商品 ASIN",
    "点击量第 2 商品 ASIN",
    "点击量第 3 商品 ASIN",
    "第 1 商品点击份额",
    "第 1 商品转化份额",
    "第 2 商品点击份额",
    "第 2 商品转化份额",
    "第 3 商品点击份额",
    "第 3 商品转化份额",
    "指标缺失原因",
    "数据来源",
    "同步状态",
]

KNOWN_RAW = {
    "st-search-frequency",
    "st-search-term",
    *(f"st-top-brand-{index}" for index in range(1, 4)),
    *(f"st-top-category-{index}" for index in range(1, 4)),
    *(f"st-top-asin-{index}" for index in range(1, 4)),
    *(f"st-top-asin-click-share-{index}" for index in range(1, 4)),
    *(f"st-top-asin-conversion-share-{index}" for index in range(1, 4)),
}


def safe_sheet_names(product_terms: list[str], week: str) -> list[str]:
    used: set[str] = {"同步状态汇总"}
    output: list[str] = []
    for index, product_term in enumerate(product_terms, 1):
        base = INVALID_SHEET.sub("-", f"{index:02d}-{product_term}-{week}").strip(" '")
        base = base or f"{index:02d}-商品词-{week}"
        candidate = base[:31]
        suffix = 2
        while candidate.casefold() in used:
            marker = f"-{suffix}"
            candidate = base[: 31 - len(marker)] + marker
            suffix += 1
        used.add(candidate.casefold())
        output.append(candidate)
    return output


def _safe_text(value: object) -> str:
    text = "" if value is None else str(value)
    if text.startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def _percent(value: Decimal | float | None) -> str:
    if value is None:
        return ""
    number = value if isinstance(value, Decimal) else Decimal(str(value))
    return f"{number * Decimal('100'):.2f}%"


def _row_values(report: AbaQueryReport, index: int, raw: dict[str, object]) -> list[str]:
    normalized = report.rows[index]
    products = normalized.products

    def product(position: int) -> AbaProduct | None:
        return products[position - 1] if len(products) >= position else None

    first, second, third = product(1), product(2), product(3)
    metric = evaluate(
        MetricInput(
            rank=normalized.search_frequency_rank,
            click=(
                Decimal(str(first.click_share))
                if first is not None and first.click_share is not None
                else None
            ),
            conversion=(
                Decimal(str(first.conversion_share))
                if first is not None and first.conversion_share is not None
                else None
            ),
            conversion2=(
                Decimal(str(second.conversion_share))
                if second is not None and second.conversion_share is not None
                else None
            ),
            conversion3=(
                Decimal(str(third.conversion_share))
                if third is not None and third.conversion_share is not None
                else None
            ),
        ),
        RuleSet(),
    )
    official_extra = [_safe_text(raw[key]) for key in raw if key not in KNOWN_RAW]
    values: list[object] = [
        normalized.search_frequency_rank,
        normalized.keyword,
        _percent(metric.purchase_ratio),
        _percent(metric.conversion_sum3),
        raw.get("st-top-brand-1") or "数据源未提供",
        raw.get("st-top-brand-2") or "数据源未提供",
        raw.get("st-top-brand-3") or "数据源未提供",
        raw.get("st-top-category-1") or "数据源未提供",
        raw.get("st-top-category-2") or "数据源未提供",
        raw.get("st-top-category-3") or "数据源未提供",
        getattr(first, "asin", ""),
        getattr(second, "asin", ""),
        getattr(third, "asin", ""),
        _percent(getattr(first, "click_share", None)),
        _percent(getattr(first, "conversion_share", None)),
        _percent(getattr(second, "click_share", None)),
        _percent(getattr(second, "conversion_share", None)),
        _percent(getattr(third, "click_share", None)),
        _percent(getattr(third, "conversion_share", None)),
        "；".join(metric.missing),
        "Seller Central 热门搜索词结构化接口",
        "已同步",
        *official_extra,
    ]
    return [_safe_text(value) for value in values]


def _letters(column: int) -> str:
    value = ""
    while column:
        column, remainder = divmod(column - 1, 26)
        value = chr(65 + remainder) + value
    return value


def _worksheet(rows: list[list[str]]) -> bytes:
    namespace = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    root = Element("worksheet", xmlns=namespace)
    data = SubElement(root, "sheetData")
    for row_number, values in enumerate(rows, 1):
        row = SubElement(data, "row", r=str(row_number))
        for column, value in enumerate(values, 1):
            cell = SubElement(row, "c", r=f"{_letters(column)}{row_number}", t="inlineStr")
            text = SubElement(SubElement(cell, "is"), "t")
            text.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
            text.text = value
    return tostring(root, encoding="utf-8", xml_declaration=True)


def build_workbook(job: HotSearchCollectionJob, reports: list[AbaQueryReport]) -> bytes:
    week = f"W{job.week_end.isocalendar().week}"
    product_terms = [task.product_term for task in job.tasks]
    names = safe_sheet_names(product_terms, week)
    report_by_term = {report.query: report for report in reports}
    sheets: list[tuple[str, list[list[str]]]] = []
    summary = [
        [
            "商品词",
            "报告周",
            "工作表名",
            "候选采集状态",
            "25 万内候选数",
            "表格同步状态",
            "失败阶段",
            "失败原因",
            "更新时间",
        ]
    ]
    for task, name in zip(job.tasks, names, strict=True):
        failure_stage = (
            "候选采集"
            if task.status == "failed"
            else ("表格同步" if task.export_status.endswith("failed") else "")
        )
        failure = task.error or task.export_error
        summary.append(
            [
                task.product_term,
                f"{job.week_start} 至 {job.week_end}",
                name,
                task.status,
                str(task.candidate_count),
                task.export_status,
                failure_stage,
                failure,
                task.updated_at.isoformat(),
            ]
        )
    sheets.append(("同步状态汇总", summary))
    for task, name in zip(job.tasks, names, strict=True):
        report = report_by_term.get(task.product_term)
        raw_rows = report.raw_rows if report is not None else []
        extras: list[str] = []
        for raw in raw_rows:
            for key in raw:
                if key not in KNOWN_RAW and key not in extras:
                    extras.append(key)
        rows = [
            [
                "报告范围=每周",
                f"周次={week}",
                f"起止日期={job.week_start} 至 {job.week_end}",
                f"商品词={task.product_term}",
                f"候选状态={task.status}",
                f"表格同步状态={task.export_status}",
            ],
            [*LEADING_HEADERS, *extras],
        ]
        if report is not None:
            for index, _row in enumerate(report.rows):
                raw = raw_rows[index] if index < len(raw_rows) else {}
                rows.append(_row_values(report, index, raw))
        sheets.append((name, rows))

    content_namespace = "http://schemas.openxmlformats.org/package/2006/content-types"
    relationship_namespace = "http://schemas.openxmlformats.org/package/2006/relationships"
    office_relationship = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    sheet_namespace = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    content = Element("Types", xmlns=content_namespace)
    SubElement(
        content,
        "Default",
        Extension="rels",
        ContentType="application/vnd.openxmlformats-package.relationships+xml",
    )
    SubElement(content, "Default", Extension="xml", ContentType="application/xml")
    SubElement(
        content,
        "Override",
        PartName="/xl/workbook.xml",
        ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
    )
    workbook = Element("workbook", xmlns=sheet_namespace)
    workbook_sheets = SubElement(workbook, "sheets")
    relationships = Element("Relationships", xmlns=relationship_namespace)
    for index, (name, _) in enumerate(sheets, 1):
        SubElement(
            workbook_sheets,
            "sheet",
            {"name": name, "sheetId": str(index), f"{{{office_relationship}}}id": f"rId{index}"},
        )
        SubElement(
            relationships,
            "Relationship",
            Id=f"rId{index}",
            Type=office_relationship + "/worksheet",
            Target=f"worksheets/sheet{index}.xml",
        )
        SubElement(
            content,
            "Override",
            PartName=f"/xl/worksheets/sheet{index}.xml",
            ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml",
        )
    output = io.BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml", tostring(content, encoding="utf-8", xml_declaration=True)
        )
        archive.writestr(
            "_rels/.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        )
        archive.writestr(
            "xl/workbook.xml", tostring(workbook, encoding="utf-8", xml_declaration=True)
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            tostring(relationships, encoding="utf-8", xml_declaration=True),
        )
        for index, (_, rows) in enumerate(sheets, 1):
            archive.writestr(f"xl/worksheets/sheet{index}.xml", _worksheet(rows))
    return output.getvalue()
