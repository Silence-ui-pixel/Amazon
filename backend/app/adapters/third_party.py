"""受限 CSV/XLSX 解析及卖家精灵字段映射；不运行公式、不推测份额单位。"""

import csv
import io
import posixpath
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

from app.domain.errors import DomainError
from app.domain.text import phrase_key
from app.domain.third_party import ImportRow
from app.domain.traffic import MetricInput, metric_snapshot

MAX_BYTES = 10 * 1024 * 1024
MAX_ROWS = 20_000
MAX_COLUMNS = 300
ALIASES: dict[str, set[str]] = {
    "keyword": {"关键词", "keyword", "keywords", "搜索词", "searchterm"},
    "translation": {"关键词翻译", "keywordcn", "translation"},
    "monthly_searches": {"月搜索量", "monthlysearches", "searches"},
    "monthly_rank": {"aba月排名", "searchrank", "abamonthlyrank"},
    "weekly_rank": {"aba周排名", "searchweeklyrank", "abaweeklyrank"},
    "bid": {"ppc竞价", "ppc价格", "bid", "ppcbid"},
    "top1_click_share": {"#1点击共享", "#1点击份额", "top1clickshare"},
    "top1_conversion_share": {"#1转化共享", "#1转化份额", "top1conversionshare"},
    "top2_conversion_share": {"#2转化共享", "#2转化份额", "top2conversionshare"},
    "top3_conversion_share": {"#3转化共享", "#3转化份额", "top3conversionshare"},
}


@dataclass
class Table:
    headers: list[str]
    rows: list[list[str]]
    numbers: list[int]


def fail(message: str) -> DomainError:
    return DomainError(422, "THIRD_PARTY_FILE_INVALID", message)


def header_key(value: str) -> str:
    return re.sub(r"[\s_\-]", "", value).casefold()


def suggest(headers: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for field, aliases in ALIASES.items():
        matches = [h for h in headers if header_key(h) in aliases]
        if len(matches) == 1:
            result[field] = matches[0]
    return result


def _xlsx(data: bytes) -> list[tuple[int, list[str]]]:
    """仅读一个非空工作表；多表必须先拆分，避免默认取错来源。"""
    with ZipFile(io.BytesIO(data)) as archive:
        if (
            len(archive.infolist()) > 1000
            or sum(x.file_size for x in archive.infolist()) > 50 * 1024 * 1024
        ):
            raise fail("XLSX 解压内容超限，请拆分词表")

        def xml(name: str) -> ET.Element:
            payload = archive.read(name)
            if b"<!DOCTYPE" in payload.upper() or b"<!ENTITY" in payload.upper():
                raise fail("XLSX 含不支持的 XML 声明")
            return ET.fromstring(payload)

        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared = ["".join(x.itertext()) for x in xml("xl/sharedStrings.xml")]
        relations = {
            x.attrib.get("Id", ""): x.attrib.get("Target", "")
            for x in xml("xl/_rels/workbook.xml.rels")
            if x.attrib.get("TargetMode") != "External"
        }
        workbook = xml("xl/workbook.xml")
        sheet_refs = [x for x in workbook.iter() if x.tag.split("}")[-1] == "sheet"]
        if len(sheet_refs) > 50:
            raise fail("XLSX 工作表过多，请拆分导出")
        sheets: list[list[tuple[int, list[str]]]] = []
        seen: set[str] = set()
        for sheet in sheet_refs:
            rid = next((v for k, v in sheet.attrib.items() if k.endswith("}id")), "")
            target = relations.get(rid, "")
            name = posixpath.normpath(
                target.lstrip("/") if target.startswith("/") else "xl/" + target
            )
            if not name.startswith("xl/worksheets/"):
                raise fail("XLSX 工作表引用无效")
            if name in seen:
                raise fail("XLSX 工作表重复引用，请重新导出")
            seen.add(name)
            result: list[tuple[int, list[str]]] = []
            for row in xml(name).iter():
                if row.tag.split("}")[-1] != "row":
                    continue
                if len(result) > MAX_ROWS:
                    raise fail("词表最多 20000 行，请拆分导出")
                cells: list[str] = []
                for cell in row:
                    reference = cell.attrib.get("r", "")
                    match = re.fullmatch(r"([A-Z]+)\d+", reference)
                    if not match:
                        raise fail("XLSX 单元格位置无效")
                    index = 0
                    for letter in match[1]:
                        index = index * 26 + ord(letter) - 64
                    if index > MAX_COLUMNS:
                        raise fail("词表最多 300 列")
                    children = {x.tag.split("}")[-1]: x for x in cell}
                    value = children.get("v")
                    text = value.text or "" if value is not None else ""
                    if "f" in children:
                        text = "=" + "".join(children["f"].itertext())
                    elif cell.attrib.get("t") == "s":
                        text = shared[int(text)]
                    elif cell.attrib.get("t") == "inlineStr":
                        text = "".join(children["is"].itertext())
                    cells.extend([""] * (index - len(cells)))
                    cells[index - 1] = text
                if any(cells):
                    result.append((int(row.attrib.get("r", str(len(result) + 1))), cells))
            if result:
                # 卖家精灵原件的辅助表有明确名称和结构，不能混入关键词主表：
                # 已见 Unique Words、Asin（批量 ASIN 清单）和 Notes 三类。
                title = sheet.attrib.get("name", "")
                header = [cell.strip() for cell in result[0][1] if cell.strip()]
                single_column = all(not any(cell.strip() for cell in row[1:]) for _, row in result)
                auxiliary_words = title == "Unique Words" and header == [
                    "词语",
                    "出现频次",
                    "百分比",
                ]
                auxiliary_asins = (
                    title == "Asin"
                    and single_column
                    and header == ["ASIN"]
                    and all(
                        len(row[0].strip()) == 10 and row[0].strip().startswith("B")
                        for _, row in result[1:]
                    )
                )
                auxiliary_notes = (
                    title == "Notes"
                    and single_column
                    and header[0:1]
                    and header[0].startswith("卖家精灵官网：https://www.sellersprite.com")
                )
                if auxiliary_words or auxiliary_asins or auxiliary_notes:
                    continue
                if sheets:
                    raise fail("请上传仅含一个非空工作表的 XLSX，每个核心词或 ASIN 分开导入")
                sheets.append(result)
        if len(sheets) != 1:
            raise fail("请上传仅含一个非空工作表的 XLSX，每个核心词或 ASIN 分开导入")
        return sheets[0]


def read_table(filename: str, data: bytes) -> Table:
    if len(data) > MAX_BYTES:
        raise DomainError(413, "THIRD_PARTY_FILE_TOO_LARGE", "词表文件不能超过 10 MiB")
    try:
        if filename.casefold().endswith(".xlsx"):
            records = _xlsx(data)
        elif filename.casefold().endswith((".csv", ".tsv")):
            try:
                text = data.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = data.decode("gb18030")
            try:
                dialect = csv.Sniffer().sniff(text[:8192], delimiters=",\t;")
                reader = csv.reader(io.StringIO(text), dialect=dialect, strict=True)
            except csv.Error:
                reader = csv.reader(io.StringIO(text), strict=True)
            records: list[tuple[int, list[str]]] = []
            for number, cells in enumerate(reader, 1):
                if len(records) > MAX_ROWS:
                    raise fail("词表最多 20000 行，请拆分导出")
                if len(cells) > MAX_COLUMNS:
                    raise fail("词表最多 300 列")
                if any(x.strip() for x in cells):
                    records.append((number, cells))
        else:
            raise fail("仅支持 CSV、TSV 或 XLSX 导出词表")
        if len(records) < 2:
            raise fail("词表须含表头和至少一行关键词")
        headers = [h.strip().lstrip("\ufeff") for h in records[0][1]]
        # 当前反查原件在两套排名后各附一列“更新时间”；按相邻原始表头区分。
        duplicates = [i for i, h in enumerate(headers) if h == "更新时间"]
        if len(duplicates) == 2 and all(i >= 2 for i in duplicates):
            first, second = duplicates
            if headers[first - 2 : first] == ["自然排名", "自然排名页码"] and headers[
                second - 2 : second
            ] == ["广告排名", "广告排名页码"]:
                headers[first] = "更新时间（自然排名）"
                headers[second] = "更新时间（广告排名）"
        if not all(headers) or len(set(map(header_key, headers))) != len(headers):
            raise fail("表头有空列或重复列，请先整理表头")
        rows: list[list[str]] = []
        numbers: list[int] = []
        for number, cells in records[1:]:
            if len(cells) > len(headers) or any(len(x) > 10000 for x in cells):
                raise fail(f"第 {number} 行字段超限或与表头不一致")
            rows.append(cells + [""] * (len(headers) - len(cells)))
            numbers.append(number)
        return Table(headers, rows, numbers)
    except DomainError:
        raise
    except (BadZipFile, KeyError, ValueError, IndexError, ET.ParseError, csv.Error, OSError):
        raise fail("文件格式损坏或编码无法识别，请重新导出 CSV/XLSX") from None


def map_rows(
    table: Table,
    mapping: dict[str, str],
    share_unit: Literal["fraction", "percent"],
    *,
    currency: str | None = None,
) -> list[ImportRow]:
    if "keyword" not in mapping or not set(mapping) <= set(ALIASES):
        raise fail("必须映射关键词列，且只能使用支持的指标字段")
    if not set(mapping.values()) <= set(table.headers) or len(set(mapping.values())) != len(
        mapping
    ):
        raise fail("映射列不存在或同一列被重复映射")
    rows: list[ImportRow] = []
    for number, cells in zip(table.numbers, table.rows, strict=True):
        raw = dict(zip(table.headers, cells, strict=True))
        values: dict[str, object] = {"row_number": number, "raw": raw}
        for field, column in mapping.items():
            value = raw[column].strip()
            if field == "keyword":
                if not value or len(value) > 1000:
                    raise fail(f"第 {number} 行关键词为空或超长")
                values[field] = value
                values["normalized_keyword"] = phrase_key(value)
                continue
            if field == "translation":
                values[field] = value or None
                continue
            if value.casefold() in ("", "-", "--", "n/a", "null"):
                values[field] = None
                continue
            if value.startswith("="):
                raise fail(f"第 {number} 行 {column} 含公式，请导出为数值")
            if field == "bid" and value.startswith("$"):
                if currency not in {"USD", "CAD", "AUD", "NZD", "SGD", "HKD"}:
                    raise fail(f"第 {number} 行 {column} 美元符号须明确匹配原表币种")
                value = value[1:].strip()
            is_share = field.endswith("_share")
            if value.endswith("%"):
                if not is_share or share_unit != "percent":
                    raise fail(f"第 {number} 行 {column} 的百分号与所选单位不一致")
                value = value[:-1]
            if not re.fullmatch(r"(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?", value):
                raise fail(f"第 {number} 行 {column} 数字格式无效，请使用非负数值")
            try:
                numeric = Decimal(value.replace(",", ""))
            except InvalidOperation:
                raise fail(f"第 {number} 行 {column} 数字无效") from None
            if is_share:
                numeric = numeric / 100 if share_unit == "percent" else numeric
                if numeric > 1:
                    raise fail(f"第 {number} 行 {column} 份额须在 0 至 100% 之间")
                values[field] = numeric
            elif field == "bid":
                values[field] = numeric
            else:
                if numeric != numeric.to_integral_value() or numeric > 10**12:
                    raise fail(f"第 {number} 行 {column} 必须为有效整数")
                if field.endswith("rank") and numeric == 0:
                    raise fail(f"第 {number} 行 {column} 排名必须大于零，未知请留空")
                values[field] = int(numeric)
        parsed = ImportRow.model_validate(values)
        rows.append(
            parsed.model_copy(
                update={
                    "traffic_metrics": metric_snapshot(
                        MetricInput(
                            rank=parsed.weekly_rank,
                            click=parsed.top1_click_share,
                            conversion=parsed.top1_conversion_share,
                            conversion2=parsed.top2_conversion_share,
                            conversion3=parsed.top3_conversion_share,
                        )
                    )
                }
            )
        )
    return rows
