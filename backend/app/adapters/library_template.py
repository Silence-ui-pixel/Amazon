"""按结果参考表生成展示和 XLSX；只读取版本快照，未接入指标留空。"""

import csv
import io
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, TypedDict, cast
from xml.etree.ElementTree import Element, SubElement, tostring
from zipfile import ZIP_DEFLATED, ZipFile

from app.adapters.third_party import ALIASES, header_key
from app.domain import taxonomy
from app.domain.library import LibraryReport
from app.domain.library_relevance import RelevanceResult
from app.domain.text import tokenize
from app.domain.traffic import RuleSet, sop_advice


class TemplateData(TypedDict):
    rule_notes: list[str]
    summary: list[list[str]]
    headers: list[str]
    rows: list[list[str | int]]


# 页面模板与 XLSX 共用列定义；未接广告平台的 CTR/CVR/花费等指标留空，不伪造为零。
HEADERS = [
    "关键词&ASIN",
    "更新日期",
    "排名",
    "流量层级",
    "词流量层级",
    "使用情况",
    "卖点词根包含",
    "场景词根包含",
    "偏好词根包含",
    "相关性",
    "垄断性",
    "购买性",
    "投放优先级",
    "投放时间",
    "应用产品",
    "当前状态",
    "否词列表",
    "CTR",
    "CVR",
    "花费",
    "订单",
    "ACOS",
    "情况说明",
    "父记录",
    "购买比",
    "点击最高商品点击份额",
    "点击最高商品转化份额",
    "前三商品转化份额合计",
    "款式相近",
    "价格差比例",
    "前半页同类占比",
    "SOP投放建议",
    "统计范围",
]


def template_data(
    report: LibraryReport, confirmed_roots: list[str] | None = None, *, ranked_only: bool = False
) -> TemplateData:
    def snapshot(key: str) -> dict[str, Any]:
        return json.loads(report.snapshots.get(key, "{}"))

    cleaning = snapshot("cleaning")
    core = snapshot("core_selections")
    competitors = snapshot("competitor_versions")
    rules = RuleSet.model_validate(snapshot("traffic").get("rules", {"id": "sop-traffic-v1"}))
    review = cast(dict[str, Any], cleaning.get("review") or {})
    roots = cast(list[dict[str, str]], review.get("roots", []))
    source = snapshot("root_source")
    selected_roots = (
        source.get("roots_snapshot", []) if confirmed_roots is None else confirmed_roots
    )
    # 三行词根只展示真正出现在本版本词库关键词里的词，并合并同一词的拼写变体。
    keyword_ngrams: set[tuple[str, ...]] = set()
    for entry in report.entries:
        tokens = tokenize(entry.keyword)
        for size in (1, 2, 3):
            for index in range(len(tokens) - size + 1):
                keyword_ngrams.add(tokens[index : index + size])

    def mentioned(term: str) -> bool:
        wanted = tokenize(term)
        if not wanted:
            return False
        if len(wanted) <= 3:
            return wanted in keyword_ngrams
        return any(
            tokenize(entry.keyword)[index : index + len(wanted)] == wanted
            for entry in report.entries
            for index in range(len(tokenize(entry.keyword)) - len(wanted) + 1)
        )

    def export_terms(category: str) -> list[str]:
        seen: set[str] = set()
        terms: list[str] = []
        for item in roots:
            if item.get("category") != category:
                continue
            term = str(item.get("term", ""))
            if not term or not mentioned(term):
                continue
            key = taxonomy.canonical(term)
            if key in seen:
                continue
            seen.add(key)
            terms.append(term)
        return terms

    # 品牌信息未入版本则留空；词根使用本版本所关联的 ABA 周报快照。
    summary: list[list[str]] = [
        ["词根", *selected_roots],
        ["品牌词", ""],
        ["竞·品牌词", ""],
        ["核心大词", *[i["keyword"] for i in core.get("items", [])]],
        [
            "竞对ASIN",
            *[i["asin"] for i in competitors.get("items", []) if i.get("decision") == "include"],
        ],
        *[
            [title, *export_terms(category)]
            for title, category in [
                ("卖点词根", "selling_point"),
                ("场景属性", "scene"),
                ("偏好属性", "preference"),
            ]
        ],
    ]
    levels = {
        "L1": "一级：3w以内",
        "L2": "二级：3-15w",
        "L3": "三级：15-30w",
        "L4": "四级：30w以上",
        "unranked": "未分层",
    }
    words = {"L1": "一级词", "L2": "二级词", "L3": "三级词", "L4": "四级词", "unranked": ""}
    usage = {
        "pending": "待使用",
        "listing": "Listing",
        "advertising": "广告",
        "both": "Listing 与广告",
        "paused": "暂停",
    }

    def flag(value: bool | None, unknown: str = "待补数据") -> str:
        return unknown if value is None else "是" if value else "否"

    def percent(value: Decimal | None) -> str:
        if value is None:
            return "待补数据"
        number = format(value * 100, "f")
        return (number.rstrip("0").rstrip(".") if "." in number else number) + "%"

    comparison = "≥" if rules.id == "sop-traffic-v2" else ">"
    rule_notes = [
        f"规则版本：{rules.id}；依据 ABA 所选周原始数据，缺失值待补，不作为否。",
        f"购买型词：点击最高商品转化份额 ÷ 同一商品点击份额 {comparison}{percent(rules.purchase_threshold)}；购买型词先投，购买比不足的词后投。",
        f"相关性高：款式相近、同类型商品价格差 ≤{percent(rules.price_delta_max)}、前半页同类结果占比 ≥{percent(rules.result_share_min)}，三项人工核对；无证据为待核对。",
        f"垄断词：点击最高商品转化份额 >{percent(rules.monopoly_first)}，或前三点击商品转化份额合计 >{percent(rules.monopoly_three)}；垄断词后投。",
        f"优先统计 ABA 排名 ≤{rules.deep_rank_max} 的词；排名更后的词作为扩展词保留。SOP建议与人工投放优先级分别记录。",
    ]

    rows: list[list[str | int]] = []
    for e in report.entries:
        if ranked_only and (e.evidence.metrics.rank is None or e.evidence.metrics.rank < 1):
            continue
        review = e.evidence.review
        updated = next(
            (x.created_at for x in reversed(report.edits) if x.data.keyword == e.keyword),
            report.published_at or report.created_at,
        )
        rows.append(
            [
                e.keyword,
                updated.strftime("%Y-%m-%d"),
                e.evidence.metrics.rank or "",
                levels[e.tier],
                words[e.tier],
                usage[e.usage],
                "、".join(e.roots.get("selling_point", [])),
                "、".join(e.roots.get("scene", [])),
                "、".join(e.roots.get("preference", [])),
                {"high": "高", "low": "低", "unknown": "待核对"}.get(review.relevance, "待核对")
                if review
                else "待核对",
                flag(e.evidence.metrics.monopoly),
                flag(e.evidence.metrics.purchase),
                {"high": "高", "normal": "普通", "low": "低"}[e.priority],
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "；".join(e.issues),
                "",
                percent(e.evidence.metrics.purchase_ratio),
                percent(e.evidence.metrics.first_click_share),
                percent(e.evidence.metrics.first_conversion_share),
                percent(e.evidence.metrics.conversion_sum3),
                flag(review.data.style_similar if review else None, "待核对"),
                percent(review.data.price_delta_ratio if review else None),
                percent(review.data.first_half_result_share if review else None),
                sop_advice(e.evidence),
                "待补排名"
                if e.evidence.metrics.rank is None
                else "优先统计（15万以内）"
                if e.evidence.metrics.deep_analysis
                else "扩展词（排名超过15万）",
            ]
        )
    return {"summary": summary, "headers": HEADERS, "rows": rows, "rule_notes": rule_notes}


def column_letters(number: int) -> str:
    letters = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


@dataclass
class AttachmentSheet:
    name: str
    headers: list[str]
    rows: list[list[str]]


def safe_sheet_name(name: str, used: set[str]) -> str:
    """Excel names are case-insensitively unique, including after truncation."""
    base = re.sub(r"[\x00-\x1f\x7f\[\]:*?/\\]", "", name).strip().strip("'") or "附表"
    candidate = base[:31].rstrip("'")
    sequence = 2
    while candidate.casefold() in used:
        suffix = f"-{sequence}"
        candidate = base[: 31 - len(suffix)] + suffix
        sequence += 1
    used.add(candidate.casefold())
    return candidate


def _source_text(value: object) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _object_table(rows: list[dict[str, Any]]) -> tuple[list[str], list[list[str]]]:
    headers = list(dict.fromkeys(key for row in rows for key in row))
    return headers, [[_source_text(row.get(key, "")) for key in headers] for row in rows]


def _share(value: str, unit: str | None) -> Decimal | None:
    text = value.strip()
    explicit_percent = text.endswith("%")
    if explicit_percent:
        text = text[:-1].strip()
    elif unit not in {"fraction", "percent"}:
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    if not number.is_finite():
        return None
    if explicit_percent or unit == "percent":
        number /= 100
    return number if 0 <= number <= 1 else None


def _percentage(value: Decimal | None) -> str:
    if value is None:
        return ""
    text = format(value * 100, "f")
    return (text.rstrip("0").rstrip(".") if "." in text else text) + "%"


def _result_key(header: str) -> str:
    return header_key(header.replace("（", "(").replace("）", ")"))


def _with_percentages(
    headers: list[str],
    rows: list[list[str]],
    *,
    unit: str | None,
    mapping: dict[str, str] | None = None,
) -> tuple[list[str], list[list[str]]]:
    fields = [
        "top1_click_share",
        "top1_conversion_share",
        "top2_conversion_share",
        "top3_conversion_share",
    ]
    api_fields = [
        "st-top-asin-click-share-1",
        "st-top-asin-conversion-share-1",
        "st-top-asin-conversion-share-2",
        "st-top-asin-conversion-share-3",
    ]
    positions: list[int | None] = []
    for field, api in zip(fields, api_fields, strict=True):
        mapped = (mapping or {}).get(field)
        if mapped is not None:
            positions.append(headers.index(mapped) if mapped in headers else None)
            continue
        positions.append(
            next(
                (
                    index
                    for index, header in enumerate(headers)
                    if header == api or header_key(header) in ALIASES[field]
                ),
                None,
            )
        )
    output_headers = list(headers)
    result_positions: list[list[int]] = []
    # 只有存在点击/转化份额来源时这两列才有意义，否则不凭空加空列。
    has_share_source = any(position is not None for position in positions)
    for title, synonyms in [
        ("购买能力", {"购买能力", "购买比"}),
        ("垄断程度（TOP3）", {"垄断程度", "垄断程度(top3)", "前三商品转化份额合计"}),
    ]:
        matches = [index for index, header in enumerate(headers) if _result_key(header) in synonyms]
        if not matches:
            if not has_share_source:
                result_positions.append([])
                continue
            matches = [len(output_headers)]
            output_headers.append(title)
        result_positions.append(matches)
    output: list[list[str]] = []
    for row in rows:
        values = list(row) + [""] * (len(output_headers) - len(row))
        shares = [
            _share(row[index], unit) if index is not None and index < len(row) else None
            for index in positions
        ]
        click, first, second, third = shares
        purchase = first / click if click is not None and click > 0 and first is not None else None
        monopoly = (
            first + second + third
            if first is not None and second is not None and third is not None
            else None
        )
        for indexes, value in zip(result_positions, [purchase, monopoly], strict=True):
            if value is None:
                # 数据源不足时保留原表内容，绝不把已有值清空。
                continue
            for index in indexes:
                values[index] = _percentage(value)
        output.append(values)
    term = next((i for i, h in enumerate(output_headers) if header_key(h) in {'搜索词','关键词','searchterm','keyword','stsearchterm'}), None)
    if term is not None:
        metric = [indexes[0] for indexes in result_positions]
        remaining = [i for i in range(len(output_headers)) if i not in metric]
        insert = remaining.index(term) + 1
        order = remaining[:insert] + metric + remaining[insert:]
        output_headers = [output_headers[i] for i in order]
        output = [[row[i] for i in order] for row in output]
    return output_headers, output


def _attachment_sheets(snapshot: dict[str, Any]) -> list[AttachmentSheet]:
    if "sheets" in snapshot:
        used = {"关键词库".casefold()}
        sheets: list[AttachmentSheet] = []
        for source in snapshot.get("sheets", []):
            records = list(csv.reader(io.StringIO(str(source.get("csv", "")), newline="")))
            if not records:
                records = [["缺失原因"], [str(source.get("reason") or "该步骤没有可导出的文件")]]
            width = max((len(row) for row in records), default=1)
            normalized = [row + [""] * (width - len(row)) for row in records]
            sheets.append(
                AttachmentSheet(
                    safe_sheet_name(str(source.get("name") or "附表"), used),
                    normalized[0],
                    normalized[1:],
                )
            )
        return sheets
    if snapshot.get("schema_version") != 1:
        return [
            AttachmentSheet(
                "来源附表缺失",
                ["缺失原因"],
                [["此历史版本未保存第二至第五步附表快照"]],
            )
        ]
    sheets: list[AttachmentSheet] = []
    used = {"关键词库".casefold()}

    def add(
        name: str,
        headers: list[str],
        rows: list[list[str]],
        *,
        status: str = "available",
        reason: str = "",
    ) -> None:
        if not rows and status != "available":
            headers, rows = ["缺失原因"], [[reason or "该来源没有可导出的数据"]]
        sheets.append(AttachmentSheet(safe_sheet_name(name, used), headers, rows))

    def aba(
        name: str,
        rows: list[dict[str, Any]],
        *,
        status: str = "available",
        reason: str = "",
    ) -> None:
        if not rows and status != "available":
            add(name, [], [], status=status, reason=reason)
            return
        headers, values = _object_table(rows)
        headers, values = _with_percentages(headers, values, unit="percent")
        add(name, headers, values)

    manifest = []
    def platform(name, source):
        item = source.get('platform_export', {})
        status = item.get('status', 'not_recorded')
        source_type = item.get('source_type', 'platform_download')
        label = '平台下载原件' if status == 'completed' and source_type == 'platform_download' else '接口采集数据（非平台原件）'
        if item.get('truncated'):
            label += '（未完整）'
        manifest.append([name, label, status,
            item.get('filename', ''), item.get('sha256', ''), item.get('error_code', ''), item.get('error', '')])
        if status == 'completed' and item.get('tables'):
            for table in item['tables']:
                label = name + ('-' + table['name'] if len(item['tables']) > 1 else '')
                prefix = table.get('prefix', [])
                if prefix:
                    records = [*prefix, table['headers'], *table['rows']]
                    width = max(map(len, records))
                    records = [row + [''] * (width - len(row)) for row in records]
                    add(label, records[0], records[1:])
                else:
                    add(label, table['headers'], table['rows'])
            return True
        return False

    for source in snapshot.get("aba_roots", []):
        if platform('第二步热门搜索词-' + source.get('query', ''), source):
            continue
        aba(
            "ABA核心大词（接口）-" + source.get("query", ""),
            source.get("rows", []),
            status=source.get("status", "available"),
            reason=source.get("reason", ""),
        )
    for source in snapshot.get("opportunity", []):
        if source.get("row_format") == "raw":
            headers = list(source.get("headers", []))
            rows = [[_source_text(value) for value in row] for row in source.get("rows", [])]
        else:
            # These are explicitly structured competitor fields, never original CSV headers.
            fields = [
                ("asin", "ASIN"),
                ("title", "标题"),
                ("price", "价格"),
                ("currency", "币种"),
                ("decision", "决策"),
                ("sources", "来源"),
                ("reason", "决策原因"),
                ("price_override_reason", "价格例外原因"),
                ("image_url", "图片链接"),
                ("price_basis", "价格依据"),
                ("price_status", "价格状态"),
                ("keyword_hits", "关键词命中数"),
                ("relevance_score", "相关性分数"),
            ]
            headers = [title for _, title in fields]
            rows = [
                [_source_text(row.get(key, "")) for key, _ in fields]
                for row in source.get("rows", [])
            ]
        status = source.get("status", "available")
        reason = source.get("reason", "")
        if rows and status != "available":
            rows = [["说明", reason], [], headers, *rows]
            headers = ["来源状态", status]
        add("商机探测器-" + source.get("query", ""), headers, rows, status=status, reason=reason)
    for source in snapshot.get("aba_expansions", []):
        produced: set[str] = set()
        query_reports = {
            item.get("query", ""): item for item in source.get("report", {}).get("queries", [])
        }
        for result in source.get("results", []):
            query = result.get("query", "")
            produced.add(query)
            name = "第四步ABA正向-" + query if source.get("direction") == "forward" else "第四步ABA反向"
            rows = result.get("raw_rows", [])
            query_report = query_reports.get(query, {})
            if platform(name, result):
                continue
            if rows:
                aba(name + "（接口）", rows)
            else:
                detail = " ".join(
                    str(value)
                    for value in (query_report.get("error_code"), query_report.get("error"))
                    if value
                )
                reason = (
                    f"{query}: {detail}"
                    if detail
                    else f"{query}：查询成功但没有原始数据行"
                )
                add(name, [], [], status="missing", reason=reason)
        queries = list(query_reports)
        if not source.get("results") and not queries:
            queries = [source.get("query", "")]
        for query in queries:
            if query in produced:
                continue
            name = "第四步ABA正向-" + query if source.get("direction") == "forward" else "第四步ABA反向"
            query_report = query_reports.get(query, {})
            detail = " ".join(
                str(value)
                for value in (query_report.get("error_code"), query_report.get("error"))
                if value
            )
            reason = (
                f"{query}: {detail}"
                if detail
                else source.get("reason", "") or f"{query}没有可导出的处理结果"
            )
            platform(name, {'platform_export': {'status': 'not_requested', 'error': reason}})
            add(name, [], [], status="missing", reason=reason)
    for source in snapshot.get("sellersprite", []):
        document = source.get("report", {})
        metadata = document.get("metadata", {})
        query = source.get("query", "")
        if source.get("direction") == "forward":
            name = "卖家精灵正向-" + query
        elif (
            metadata.get("collection_method") == "traffic_extension"
            or len(metadata.get("source_queries", [])) > 1
        ):
            name = "卖家精灵反向-多个ASIN"
        else:
            name = "卖家精灵反向-" + query
        if platform(name, source):
            continue
        name += "（接口）"
        raw = source.get("raw_document", {})
        confirmation = cast(dict[str, Any], document.get("confirmation") or {})
        headers, rows = _with_percentages(
            list(raw.get("headers", [])),
            [[_source_text(value) for value in row] for row in raw.get("rows", [])],
            unit=confirmation.get("share_unit"),
            mapping=confirmation.get("mapping"),
        )
        add(
            name,
            headers,
            rows,
            status=source.get("status", "available"),
            reason=source.get("reason", ""),
        )
    add('平台下载与附表来源清单', ['附表','数据来源','下载状态','平台文件名','SHA256','失败代码','失败或进行中说明'], manifest)
    return sheets


def freeze_attachment_exports(snapshot_text: str) -> str:
    """Save processed attachment tables as CSV artifacts for direct final assembly."""
    snapshot = cast(dict[str, Any], json.loads(snapshot_text))
    if "sheets" in snapshot:
        return snapshot_text
    exports: list[dict[str, str]] = []
    for sheet in _attachment_sheets(snapshot):
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(sheet.headers)
        writer.writerows(sheet.rows)
        exports.append({"name": sheet.name, "csv": stream.getvalue()})
    snapshot["sheets"] = exports
    return json.dumps(snapshot, ensure_ascii=False)


def attachment_sheets(report: LibraryReport) -> list[AttachmentSheet]:
    """Copy the processed step exports stored with this library version."""
    snapshot = cast(
        dict[str, Any], json.loads(report.snapshots.get("workbook_attachments", "{}"))
    )
    return _attachment_sheets(snapshot)


def _worksheet(
    rows: list[list[str | int]], *, width: int, main: bool,
    numeric_percentages: dict[tuple[int, int], Decimal] | None = None,
) -> bytes:
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    root = Element("worksheet", xmlns=ns)
    if main:
        views = SubElement(root, "sheetViews")
        view = SubElement(views, "sheetView", workbookViewId="0")
        SubElement(view, "pane", ySplit="11", topLeftCell="A12", state="frozen")
    cols = SubElement(root, "cols")
    SubElement(cols, "col", min="1", max="1", width="44", customWidth="1")
    if width > 1:
        SubElement(cols, "col", min="2", max=str(width), width="20", customWidth="1")
    sheet = SubElement(root, "sheetData")
    for index, values in enumerate(rows, 1):
        row = SubElement(sheet, "row", r=str(index))
        for col, value in enumerate(values, 1):
            letters = column_letters(col)
            if numeric_percentages is not None and (index, col) in numeric_percentages:
                cell = SubElement(row, "c", r=f"{letters}{index}", t="n", s="1")
                SubElement(cell, "v").text = format(numeric_percentages[index, col], "f")
                continue
            # inlineStr 防止等号开头的用户内容被当成公式执行。
            cell = SubElement(row, "c", r=f"{letters}{index}", t="inlineStr")
            text = SubElement(SubElement(cell, "is"), "t")
            text.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
            text.text = _spreadsheetml_text(str(value))
    if main:
        SubElement(root, "autoFilter", ref=f"A11:{column_letters(width)}{len(rows)}")
    return tostring(root, encoding="utf-8", xml_declaration=True)


def _spreadsheetml_text(value: str) -> str:
    """Encode Excel escapes before XML serialization without changing source text."""
    escaped = re.sub(
        r"(?i)_x[0-9a-f]{4}_",
        lambda match: "_x005F_" + match.group(0)[1:],
        value,
    )
    output: list[str] = []
    for character in escaped:
        codepoint = ord(character)
        valid_xml = (
            character in ("\t", "\n", "\r")
            or 0x20 <= codepoint <= 0xD7FF
            or 0xE000 <= codepoint <= 0xFFFD
            or 0x10000 <= codepoint <= 0x10FFFF
        )
        if character == "\r" or not valid_xml:
            output.append(f"_x{codepoint:04X}_")
        else:
            output.append(character)
    return "".join(output)


def export_xlsx(
    report: LibraryReport, confirmed_roots: list[str] | None = None, *, ranked_only: bool = False,
    relevance_results: dict[str, RelevanceResult] | None = None,
) -> bytes:
    data = template_data(report, confirmed_roots, ranked_only=ranked_only)
    if relevance_results is not None:
        for row in data["rows"]:
            result = relevance_results[str(row[0])]
            if result.ratio is None or result.status != "completed":
                raise ValueError("Excel relevance results must be complete")
            # Keep all original rows/columns; replace only relevance and append evidence notes.
            row[HEADERS.index("相关性")] = ""
            note = (
                f"相关性=高相关商品数/有效核验商品数：{result.high_related_count}/{result.valid_samples}；"
                f"采集{result.sampled}个，目标30个；单商品综合分>60；规则library-relevance-v1。"
                "原人工相关性证据及SOP建议沿用原口径。"
            )
            index = HEADERS.index("情况说明")
            row[index] = "；".join(part for part in (str(row[index]), note) if part)
    rows: list[list[str | int]] = [
        ["关键词库"],
        *[list(r) for r in data["summary"]],
        [],
        list(data["headers"]),
        *data["rows"],
    ]
    percentages: dict[tuple[int, int], Decimal] = {}
    if relevance_results is not None:
        first_data_row = len(rows) - len(data["rows"]) + 1
        for offset, row in enumerate(data["rows"]):
            ratio = relevance_results[str(row[0])].ratio
            assert ratio is not None
            percentages[first_data_row + offset, HEADERS.index("相关性") + 1] = ratio
    worksheets = [("关键词库", rows, len(HEADERS))]
    for attachment in attachment_sheets(report):
        values: list[list[str | int]] = [
            list(attachment.headers),
            *[list(r) for r in attachment.rows],
        ]
        worksheets.append((attachment.name, values, max((len(r) for r in values), default=1)))
    content_ns = "http://schemas.openxmlformats.org/package/2006/content-types"
    rel_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    office_rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    sheet_ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    content = Element("Types", xmlns=content_ns)
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
    if relevance_results is not None:
        SubElement(content, "Override", PartName="/xl/styles.xml",
                   ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml")
    workbook = Element("workbook", xmlns=sheet_ns)
    sheets = SubElement(workbook, "sheets")
    relationships = Element("Relationships", xmlns=rel_ns)
    if relevance_results is not None:
        SubElement(relationships, "Relationship", Id="rIdStyles",
                   Type=office_rel + "/styles", Target="styles.xml")
    for index, (name, _, _) in enumerate(worksheets, 1):
        SubElement(
            sheets,
            "sheet",
            {"name": name, "sheetId": str(index), f"{{{office_rel}}}id": f"rId{index}"},
        )
        SubElement(
            relationships,
            "Relationship",
            Id=f"rId{index}",
            Type=office_rel + "/worksheet",
            Target=f"worksheets/sheet{index}.xml",
        )
        SubElement(
            content,
            "Override",
            PartName=f"/xl/worksheets/sheet{index}.xml",
            ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml",
        )
    out = io.BytesIO()
    with ZipFile(out, "w", ZIP_DEFLATED) as z:
        if relevance_results is not None:
            z.writestr("xl/styles.xml", _relevance_styles())
        z.writestr("[Content_Types].xml", tostring(content, encoding="utf-8", xml_declaration=True))
        z.writestr(
            "_rels/.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        )
        z.writestr("xl/workbook.xml", tostring(workbook, encoding="utf-8", xml_declaration=True))
        z.writestr(
            "xl/_rels/workbook.xml.rels",
            tostring(relationships, encoding="utf-8", xml_declaration=True),
        )
        for index, (_, values, width) in enumerate(worksheets, 1):
            z.writestr(
                f"xl/worksheets/sheet{index}.xml",
                _worksheet(values, width=max(width, 1), main=index == 1,
                           numeric_percentages=percentages if index == 1 else None),
            )
    return out.getvalue()


def _relevance_styles() -> bytes:
    """Only the new relevance data cells opt into the custom percentage format."""
    return b'''<?xml version="1.0" encoding="UTF-8"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<numFmts count="1"><numFmt numFmtId="164" formatCode="0.0%"/></numFmts>
<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>
<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>
<borders count="1"><border/></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/></cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>'''
