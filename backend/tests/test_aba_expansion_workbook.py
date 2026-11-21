"""第四步 ABA 正向逐核心词工作簿。"""

import io
from datetime import date
from xml.etree import ElementTree
from zipfile import ZipFile

from app.adapters.aba import normalize_row
from app.adapters.aba_expansion_workbook import build_query_workbook, query_filename
from app.domain.aba import AbaQueryReport

NS = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def table(content: bytes) -> list[list[str]]:
    with ZipFile(io.BytesIO(content)) as archive:
        root = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    output: list[list[str]] = []
    for row in root.findall(".//x:sheetData/x:row", NS):
        output.append(
            ["".join(cell.itertext()) for cell in row.findall("x:c", NS)]
        )
    return output


def report(
    raw_rows: list[dict[str, object]], *, legacy: bool = False, query: str = "dumbbell/rack [home]"
) -> AbaQueryReport:
    rows = [normalize_row(raw) for raw in raw_rows]
    if legacy:
        rows = [row.model_copy(update={"traffic_metrics": None}) for row in rows]
    result = AbaQueryReport(
        query=query,
        query_mode="root",
        total=len(rows),
        fetched=len(rows),
        pages=1,
        truncated=False,
        stop_reason="complete",
        rows=rows,
    )
    result.raw_rows = raw_rows
    return result


def test_query_workbook_places_metrics_after_search_term_and_preserves_raw_columns() -> None:
    raw = {
        "st-search-frequency": "120000",
        "st-search-term": "dumbbell rack",
        "st-top-asin-1": "=1+1",
        "st-top-asin-click-share-1": "20%",
        "st-top-asin-conversion-share-1": "13%",
        "st-top-asin-2": "B000000002",
        "st-top-asin-conversion-share-2": "10%",
        "st-top-asin-3": "B000000003",
        "st-top-asin-conversion-share-3": "5%",
        "official-extra": "kept",
    }

    values = table(
        build_query_workbook(
            report([raw]), date(2026, 10, 4), date(2026, 10, 10)
        )
    )

    assert values[0][:3] == [
        "报告范围=每周",
        "起止日期=2026-10-04 至 2026-10-10",
        "查询核心大词=dumbbell/rack [home]",
    ]
    assert values[1][:4] == [
        "搜索频率排名",
        "搜索词",
        "购买能力（>60%）",
        "垄断程度（TOP3 累计>35%）",
    ]
    assert values[2][:4] == ["120000", "dumbbell rack", "65.00%", "28.00%"]
    assert values[2][values[1].index("official-extra")] == "kept"
    assert values[2][values[1].index("点击量第 1 商品 ASIN")] == "=1+1"


def test_query_workbook_legacy_rows_recompute_without_compressing_missing_first_slot() -> None:
    raw = {
        "st-search-frequency": "120000",
        "st-search-term": "dumbbell rack",
        "st-top-asin-1": "-",
        "st-top-asin-2": "B000000002",
        "st-top-asin-click-share-2": "20%",
        "st-top-asin-conversion-share-2": "13%",
        "st-top-asin-3": "B000000003",
        "st-top-asin-conversion-share-3": "5%",
    }

    values = table(
        build_query_workbook(
            report([raw], legacy=True), date(2026, 10, 4), date(2026, 10, 10)
        )
    )

    assert values[2][2] == ""
    assert values[2][3] == ""
    assert "首位点击份额缺失" in values[2][values[1].index("指标缺失原因")]


def test_query_filename_and_sheet_name_are_safe_for_chinese_and_illegal_characters() -> None:
    result = report(
        [
            {
                "st-search-frequency": "1",
                "st-search-term": "safe",
            }
        ],
        legacy=True,
    )

    filename = query_filename(result.query)
    content = build_query_workbook(result, date(2026, 10, 4), date(2026, 10, 10))
    with ZipFile(io.BytesIO(content)) as archive:
        workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        sheet = workbook.find(".//x:sheet", NS)

    assert filename == "第四步ABA正向dumbbell-rack [home].xlsx"
    assert sheet is not None
    assert len(sheet.attrib["name"]) <= 31
    assert all(char not in sheet.attrib["name"] for char in "[]:*?/\\")


def test_query_filename_preserves_legal_characters_and_avoids_long_name_collisions() -> None:
    assert query_filename("  .dumbbell  rack.  ") == "第四步ABA正向  .dumbbell  rack.  .xlsx"

    first = query_filename("长" * 260 + "甲")
    second = query_filename("长" * 260 + "乙")

    assert first != second
    assert len(first.encode("utf-16-le")) // 2 <= 255
    assert len(second.encode("utf-16-le")) // 2 <= 255


def test_sheet_name_removes_xml_controls_and_never_ends_with_an_apostrophe() -> None:
    result = report(
        [{"st-search-frequency": "1", "st-search-term": "safe"}],
        legacy=True,
        query="rack\x00" + "a" * 25 + "'tail",
    )

    content = build_query_workbook(result, date(2026, 10, 4), date(2026, 10, 10))
    with ZipFile(io.BytesIO(content)) as archive:
        workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        sheet = workbook.find(".//x:sheet", NS)

    assert sheet is not None
    assert "\x00" not in sheet.attrib["name"]
    assert not sheet.attrib["name"].endswith("'")


def test_reverse_workbook_keeps_all_competitor_asins_and_reuses_metrics() -> None:
    asins = "B012345678,B087654321"
    raw = {
        "st-search-frequency": "120000",
        "st-search-term": "dumbbell rack",
        "st-top-asin-1": "B000000001",
        "st-top-asin-click-share-1": "20%",
        "st-top-asin-conversion-share-1": "13%",
        "st-top-asin-2": "B000000002",
        "st-top-asin-conversion-share-2": "10%",
        "st-top-asin-3": "B000000003",
        "st-top-asin-conversion-share-3": "5%",
    }
    reverse = report([raw], query=asins)

    assert query_filename(asins, direction="reverse") == "第四步ABA反向2个ASIN.xlsx"
    content = build_query_workbook(
        reverse,
        date(2026, 10, 4),
        date(2026, 10, 10),
        direction="reverse",
    )
    values = table(content)
    with ZipFile(io.BytesIO(content)) as archive:
        workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        sheet = workbook.find(".//x:sheet", NS)

    assert values[0][2] == f"查询竞对ASIN={asins}"
    assert values[1][:4] == [
        "搜索频率排名",
        "搜索词",
        "购买能力（>60%）",
        "垄断程度（TOP3 累计>35%）",
    ]
    assert values[2][:4] == ["120000", "dumbbell rack", "65.00%", "28.00%"]
    assert sheet is not None
    assert sheet.attrib["name"].startswith("ABA反向-")
