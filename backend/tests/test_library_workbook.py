"""工作簿附表必须固定清洗实际采用的来源和原始字段。"""

import hashlib
import io
import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest
from sqlalchemy import Engine
from test_library import ready_library
from test_third_party import MAPPING, preview

from app.adapters.library_template import export_xlsx
from app.db.repository import Repository
from app.domain.cleaning import CleaningReport
from app.domain.library import LibraryReport, LibraryRequest
from app.services.library import LibraryService
from app.services.traffic import TrafficService

NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def workbook_report(attachments: dict[str, Any] | None) -> LibraryReport:
    return LibraryReport(
        id=uuid4(),
        project_id=uuid4(),
        run_id=uuid4(),
        created_at=datetime.now(UTC),
        request=LibraryRequest(traffic_id=uuid4(), traffic_version=1),
        source_sha256="test",
        snapshots=(
            {"workbook_attachments": json.dumps({"schema_version": 1, **attachments})}
            if attachments is not None
            else {}
        ),
        entries=[],
        suggestions=[],
    )


def test_template_xlsx_copies_processed_exports_without_rebuilding_columns() -> None:
    """A final export must copy step artifacts, not infer or recalculate their fields."""
    report = LibraryReport(
        id=uuid4(),
        project_id=uuid4(),
        run_id=uuid4(),
        created_at=datetime.now(UTC),
        request=LibraryRequest(traffic_id=uuid4(), traffic_version=1),
        source_sha256="test",
        snapshots={
            "workbook_attachments": json.dumps(
                {
                    "schema_version": 2,
                    "sheets": [
                        {
                            "name": "卖家精灵正向-dumbbell rack",
                            "csv": (
                                "关键词,购买性,垄断性,原字段\r\n"
                                "dumbbell rack,60%,10%,  保留空格  \r\n"
                            ),
                        }
                    ],
                },
                ensure_ascii=False,
            )
        },
        entries=[],
        suggestions=[],
    )

    tables = workbook_tables(export_xlsx(report))

    assert tables["卖家精灵正向-dumbbell rack"] == [
        ["关键词", "购买性", "垄断性", "原字段"],
        ["dumbbell rack", "60%", "10%", "  保留空格  "],
    ]


def workbook_tables(payload: bytes) -> dict[str, list[list[str]]]:
    with ZipFile(io.BytesIO(payload)) as archive:
        # Every dynamic package relationship and content-type must resolve.
        for name in archive.namelist():
            ET.fromstring(archive.read(name))
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {item.attrib["Id"]: "xl/" + item.attrib["Target"] for item in relationships}
        types = ET.fromstring(archive.read("[Content_Types].xml"))
        overrides = {item.attrib.get("PartName") for item in types}
        tables: dict[str, list[list[str]]] = {}
        for sheet in workbook.findall("s:sheets/s:sheet", NS):
            rid = sheet.attrib[
                "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
            ]
            target = targets[rid]
            assert "/" + target in overrides
            xml = ET.fromstring(archive.read(target))
            assert not xml.findall(".//s:f", NS)
            def cell_text(cell: ET.Element) -> str:
                if cell.attrib.get("t") == "inlineStr":
                    return cell.findtext("s:is/s:t", default="", namespaces=NS)
                value = Decimal(cell.findtext("s:v", default="0", namespaces=NS))
                text = format(value * 100, "f").rstrip("0").rstrip(".")
                return text + "%"
            tables[sheet.attrib["name"]] = [
                [cell_text(cell) for cell in row]
                for row in xml.findall("s:sheetData/s:row", NS)
            ]
        return tables


def test_template_xlsx_includes_processed_source_sheets() -> None:
    # Dropping a source, using normalized rows, filtering attachments, or splitting
    # merged ASIN evidence must break the actual generated package contract.
    aba_row = {
        "st-search-term": "unranked source word",
        "st-top-asin-click-share-1": 10,
        "st-top-asin-conversion-share-1": "6%",
        "st-top-asin-conversion-share-2": 3,
        "st-top-asin-conversion-share-3": 1,
        "extra": {"zero": 0, "missing": None},
    }
    seller_headers = [
        "关键词",
        "原样字段",
        "top1_click_share",
        "top1_conversion_share",
        "top2_conversion_share",
        "top3_conversion_share",
        "购买能力",
        "垄断程度(TOP3)",
    ]
    seller = {
        "direction": "forward",
        "query": "cooling blanket",
        "report": {"confirmation": {"share_unit": "fraction"}},
        "raw_document": {
            "headers": seller_headers,
            "rows": [
                ["not ranked", "  原值  ", "0.1", "0.06", "0.03", "0.01", "old", "old"],
                ["=1+1", "@text", "0", "0.06", "0.03", "0.01", "old", "old"],
            ],
        },
    }
    attachments = {
        "aba_roots": [{"query": "blanket", "rows": [aba_row, {"st-search-term": "all rows"}]}],
        "opportunity": [
            {
                "query": "cooling blanket",
                "row_format": "raw",
                "headers": ["ASIN", "ASIN", "原字段"],
                "rows": [["B012345678", "duplicate", "selected"], ["B099999999", "", "excluded"]],
            },
            {
                "query": "structured",
                "row_format": "structured",
                "rows": [
                    {
                        "asin": "B012345678",
                        "title": "title",
                        "price": "12.30",
                        "currency": "USD",
                        "decision": "exclude",
                        "sources": [{"kind": "manual"}],
                    }
                ],
            },
        ],
        "aba_expansions": [
            {
                "direction": "forward",
                "query": "全部查询",
                "results": [
                    {"query": "cooling", "raw_rows": [aba_row]},
                    {"query": "blanket", "raw_rows": [aba_row]},
                ],
            },
            {"direction": "reverse", "results": [{"query": "B012345678", "raw_rows": [aba_row]}]},
        ],
        "sellersprite": [
            seller,
            {**seller, "direction": "reverse", "query": "B012345678"},
            {
                **seller,
                "direction": "reverse",
                "query": "B012345678,B087654321",
                "report": {
                    "confirmation": {"share_unit": "fraction"},
                    "metadata": {
                        "collection_method": "traffic_extension",
                        "source_queries": ["B012345678", "B087654321"],
                    },
                },
            },
        ],
    }
    report = workbook_report(attachments)
    tables = workbook_tables(export_xlsx(report, ranked_only=True))
    assert list(tables) == [
        "关键词库",
        "ABA核心大词-blanket",
        "商机探测器-cooling blanket",
        "商机探测器-structured",
        "ABA正向-cooling",
        "ABA正向-blanket",
        "ABA反向",
        "卖家精灵正向-cooling blanket",
        "卖家精灵反向-B012345678",
        "卖家精灵反向-多个ASIN",
    ]
    aba = tables["ABA核心大词-blanket"]
    assert [h for h in aba[0] if h not in {"购买能力", "垄断程度（TOP3）"}] == list(aba_row)
    assert aba[1][aba[0].index("购买能力")] == "60%"
    assert aba[1][aba[0].index("垄断程度（TOP3）")] == "10%"
    assert json.loads(aba[1][aba[0].index("extra")]) == {"zero": 0, "missing": None}
    assert aba[2][0] == "all rows"
    assert tables["商机探测器-cooling blanket"] == [
        ["ASIN", "ASIN", "原字段"],
        ["B012345678", "duplicate", "selected"],
        ["B099999999", "", "excluded"],
    ]
    structured = tables["商机探测器-structured"]
    assert structured[0][:6] == ["ASIN", "标题", "价格", "币种", "决策", "来源"]
    assert structured[1][:5] == ["B012345678", "title", "12.30", "USD", "exclude"]
    assert json.loads(structured[1][5]) == [{"kind": "manual"}]
    for name in ["ABA正向-cooling", "ABA正向-blanket", "ABA反向"]:
        assert tables[name][1] == aba[1]
    for name in [
        "卖家精灵正向-cooling blanket",
        "卖家精灵反向-B012345678",
        "卖家精灵反向-多个ASIN",
    ]:
        table = tables[name]
        assert table[0] == seller_headers
        assert len(table) == 3
        assert table[1] == ["not ranked", "  原值  ", "0.1", "0.06", "0.03", "0.01", "60%", "10%"]
        assert table[2][-2:] == ["", "10%"]
    assert workbook_tables(export_xlsx(report, ranked_only=False)) == tables


def test_attachment_sheet_names_and_cells_are_excel_safe() -> None:
    # Invalid/truncated duplicate names and interpreting source text as formulas
    # would make this consumer fail to open a safe, faithful workbook.
    dangerous = ["=SUM(1,1)", "+12", "-4", "@name", '<xml>&"', "  whitespace  "]
    rows = [
        {
            "st-search-term": value,
            "st-top-asin-click-share-1": 0,
            "st-top-asin-conversion-share-1": 6,
            "st-top-asin-conversion-share-2": 3,
            "st-top-asin-conversion-share-3": None,
        }
        for value in dangerous
    ]
    report = workbook_report(
        {
            "aba_roots": [
                {"query": query, "rows": rows}
                for query in [
                    "x" * 60,
                    "x" * 60 + "other",
                    '[]:*?/\\<xml>&"',
                    "",
                    "CASE",
                    "case",
                ]
            ],
            "aba_expansions": [
                {"direction": "forward", "results": [{"query": "no raw", "rows": []}]}
            ],
        }
    )
    tables = workbook_tables(export_xlsx(report))
    assert "SOP规则说明" not in tables
    names = list(tables)
    assert len(names) >= 7
    assert len({name.casefold() for name in names}) == len(names)
    assert all(0 < len(name) <= 31 and not any(c in name for c in "[]:*?/\\") for name in names)
    assert names[1] == "ABA核心大词-" + "x" * 23
    assert names[2] == "ABA核心大词-" + "x" * 21 + "-2"
    for table in list(tables.values())[1:7]:
        assert [row[0] for row in table[1:]] == dangerous
        assert all(row[table[0].index("购买能力")] == "" for row in table[1:])
        assert all(row[table[0].index("垄断程度（TOP3）")] == "" for row in table[1:])
    assert list(workbook_tables(export_xlsx(workbook_report(None)))) == [
        "关键词库",
        "来源附表缺失",
    ]


def test_attachment_cells_encode_spreadsheetml_control_characters() -> None:
    original = ["first\x0bsecond", "first\r\nsecond", "literal _x000B_ text"]
    report = workbook_report(
        {
            "aba_roots": [
                {
                    "query": "controls",
                    "rows": [{"st-search-term": value} for value in original],
                }
            ]
        }
    )

    payload = export_xlsx(report)
    tables = workbook_tables(payload)

    def decode_spreadsheetml(value: str) -> str:
        return re.sub(
            r"_x([0-9A-Fa-f]{4})_",
            lambda match: chr(int(match.group(1), 16)),
            value,
        )

    encoded = [row[0] for row in tables["ABA核心大词-controls"][1:]]
    assert [decode_spreadsheetml(value) for value in encoded] == original
    assert encoded == ["first_x000B_second", "first_x000D_\nsecond", "literal _x005F_x000B_ text"]


def test_missing_sources_create_empty_reason_sheets() -> None:
    report = workbook_report(
        {
            "aba_roots": [
                {
                    "query": "dumbbell",
                    "status": "empty",
                    "reason": "该词根查询没有原始数据行",
                    "rows": [],
                }
            ],
            "opportunity": [
                {
                    "query": "dumbbell rack",
                    "status": "missing",
                    "reason": "所选版本的归档 CSV 文件不存在",
                    "row_format": "raw",
                    "headers": [],
                    "rows": [],
                }
            ],
            "aba_expansions": [
                {
                    "direction": "forward",
                    "query": "all",
                    "status": "failed",
                    "reason": "dumbbell rack: ABA_TIMEOUT 采集超时",
                    "report": {"queries": [{"query": "dumbbell rack"}]},
                    "results": [],
                },
                {
                    "direction": "reverse",
                    "query": "all",
                    "status": "missing",
                    "reason": "清洗选中的来源记录不存在",
                    "report": {},
                    "results": [],
                },
            ],
            "sellersprite": [
                {
                    "direction": "forward",
                    "query": "dumbbell rack",
                    "status": "empty",
                    "reason": "所选来源查询成功但没有数据行",
                    "report": {"metadata": {}},
                    "raw_document": {"headers": [], "rows": []},
                },
                {
                    "direction": "reverse",
                    "query": "B012345678,B087654321",
                    "status": "missing",
                    "reason": "清洗报告未选择该方向的来源",
                    "report": {
                        "metadata": {
                            "collection_method": "traffic_extension",
                            "source_queries": ["B012345678", "B087654321"],
                        }
                    },
                    "raw_document": {},
                },
            ],
        }
    )

    tables = workbook_tables(export_xlsx(report))
    expected = {
        "ABA核心大词-dumbbell": "该词根查询没有原始数据行",
        "商机探测器-dumbbell rack": "所选版本的归档 CSV 文件不存在",
        "ABA正向-dumbbell rack": "dumbbell rack: ABA_TIMEOUT 采集超时",
        "ABA反向": "清洗选中的来源记录不存在",
        "卖家精灵正向-dumbbell rack": "所选来源查询成功但没有数据行",
        "卖家精灵反向-多个ASIN": "清洗报告未选择该方向的来源",
    }
    for name, reason in expected.items():
        assert tables[name] == [["缺失原因"], [reason]]

    legacy = workbook_tables(export_xlsx(workbook_report(None)))
    assert list(legacy) == ["关键词库", "来源附表缺失"]
    assert legacy["来源附表缺失"] == [["缺失原因"], ["此历史版本未保存第二至第五步附表快照"]]


def test_sheet_names_remove_xml_control_characters() -> None:
    report = workbook_report(
        {"aba_roots": [{"query": "bad\x0bname", "rows": [{"st-search-term": "ok"}]}]}
    )
    tables = workbook_tables(export_xlsx(report))
    assert "ABA核心大词-badname" in tables


def test_aba_batch_creates_reason_sheet_for_each_expected_query() -> None:
    report = workbook_report(
        {
            "aba_expansions": [
                {
                    "direction": "forward",
                    "status": "available",
                    "reason": "",
                    "report": {
                        "queries": [
                            {"query": "has rows", "error_code": None, "error": None},
                            {"query": "empty", "error_code": None, "error": None},
                            {"query": "failed", "error_code": "TIMEOUT", "error": "采集超时"},
                        ]
                    },
                    "results": [
                        {"query": "has rows", "raw_rows": [{"st-search-term": "one"}]},
                        {"query": "empty", "raw_rows": []},
                    ],
                }
            ]
        }
    )
    tables = workbook_tables(export_xlsx(report))
    assert tables["ABA正向-has rows"][1][0] == "one"
    assert tables["ABA正向-empty"] == [["缺失原因"], ["empty：查询成功但没有原始数据行"]]
    assert tables["ABA正向-failed"] == [["缺失原因"], ["failed: TIMEOUT 采集超时"]]


def test_opportunity_fallback_keeps_rows_and_displays_reason() -> None:
    report = workbook_report(
        {
            "opportunity": [
                {
                    "query": "rack",
                    "status": "fallback",
                    "reason": "归档 CSV 不存在，以下为结构化竞对数据",
                    "row_format": "structured",
                    "rows": [{"asin": "B012345678", "title": "Rack"}],
                }
            ]
        }
    )
    table = workbook_tables(export_xlsx(report))["商机探测器-rack"]
    assert table[:3] == [
        ["来源状态", "fallback"],
        ["说明", "归档 CSV 不存在，以下为结构化竞对数据"],
        [],
    ]
    assert table[3][0] == "ASIN" and table[4][0] == "B012345678"


def test_processed_percentages_are_copied_without_style_rewrite() -> None:
    report = workbook_report(
        {"sheets": [{"name": "处理结果", "csv": "关键词,购买性,垄断性\r\nrack,60%,10%\r\n"}]}
    )
    payload = export_xlsx(report)
    assert workbook_tables(payload)["处理结果"] == [
        ["关键词", "购买性", "垄断性"],
        ["rack", "60%", "10%"],
    ]


@pytest.mark.parametrize(
    "unit,values,expected",
    [
        ("percent", ["10", "6", "3", "1"], ["60%", "10%"]),
        ("fraction", ["0.1", "0.06", "0.03", "0.01"], ["60%", "10%"]),
        (None, ["10%", "6%", "3%", "1%"], ["60%", "10%"]),
        (None, ["0.1", "0.06", "0.03", "0.01"], ["", ""]),
        ("fraction", ["NaN", "0.06", "0.03", "0.01"], ["", "10%"]),
        ("percent", ["10", "unknown", "3", "1"], ["", ""]),
    ],
)
def test_attachment_percentages_use_pinned_unit_and_mapping(
    unit: str | None, values: list[str], expected: list[str]
) -> None:
    # Guessing units from magnitude, ignoring user-confirmed mappings, or
    # duplicating existing equivalent result columns loses source semantics.
    headers = ["关键词", "A", "B", "C", "D", "购买比", "前三商品转化份额合计"]
    report = workbook_report(
        {
            "sellersprite": [
                {
                    "query": "mapped",
                    "direction": "forward",
                    "report": {
                        "confirmation": {
                            "share_unit": unit,
                            "mapping": {
                                "top1_click_share": "A",
                                "top1_conversion_share": "B",
                                "top2_conversion_share": "C",
                                "top3_conversion_share": "D",
                            },
                        }
                    },
                    "raw_document": {"headers": headers, "rows": [["word", *values, "old", "old"]]},
                }
            ]
        }
    )
    table = workbook_tables(export_xlsx(report))["卖家精灵正向-mapped"]
    assert table[0] == headers
    assert table[1] == ["word", *values, *expected]


def test_attachment_confirmed_mapping_takes_precedence_over_header_aliases() -> None:
    # The source's confirmed mapping must win when the file also contains a
    # different column whose name happens to match a built-in alias.
    report = workbook_report(
        {
            "sellersprite": [
                {
                    "query": "mapped",
                    "direction": "forward",
                    "report": {
                        "confirmation": {
                            "share_unit": "percent",
                            "mapping": {
                                "top1_click_share": "confirmed click",
                                "top1_conversion_share": "confirmed conversion",
                                "top2_conversion_share": "#2转化份额",
                                "top3_conversion_share": "#3转化共享",
                            },
                        }
                    },
                    "raw_document": {
                        "headers": [
                            "关键词",
                            "#1 点击共享",
                            "#1转化共享",
                            "confirmed click",
                            "confirmed conversion",
                            "#2转化份额",
                            "#3转化共享",
                        ],
                        "rows": [["word", "50", "50", "10", "6", "3", "1"]],
                    },
                }
            ]
        }
    )
    table = workbook_tables(export_xlsx(report))["卖家精灵正向-mapped"]
    assert table[1][-2:] == ["60%", "10%"]


@pytest.mark.parametrize("archive_available", [False, True])
@pytest.mark.parametrize("batch", [False, True])
def test_library_snapshot_pins_processed_attachment_sources(
    db: str, engine: Engine, tmp_path: Path, archive_available: bool, batch: bool
) -> None:
    # Removing the snapshot, reading latest sources, dropping raw columns, or splitting
    # a batch import into individual ASINs must each break this consumer contract.
    with ready_library(db, engine, tmp_path) as (api, base, _database_url, request):
        if batch:
            imported = preview(
                api,
                base + "/third-party",
                "B012345678,B087654321",
                "reverse",
                recent=True,
                metadata_extra={
                    "collection_method": "traffic_extension",
                    "source_queries": ["B012345678", "B087654321"],
                    "variant_scope": "all",
                },
            )
            confirmed = api.post(
                base + f"/third-party/{imported['id']}/confirm",
                json={"mapping": MAPPING, "share_unit": "percent"},
            )
            assert confirmed.status_code == 200, confirmed.text
            clean = api.post(
                base + "/cleaning",
                json={
                    "aba_week_end": "2026-10-17",
                    "seller_period": "last_30_days",
                    "text_only_acknowledged": True,
                },
            ).json()
            reviewed = api.put(
                base + f"/cleaning/{clean['id']}/review",
                json={
                    "expected_version": 0,
                    "roots": [{"term": "portable", "category": "preference"}],
                    "exclusions": [{"keyword": "=1+1", "reason": "噪声"}],
                    "reason": "核对",
                },
            )
            assert reviewed.status_code == 200, reviewed.text
            traffic = api.post(
                base + "/traffic",
                json={
                    "cleaning_id": clean["id"],
                    "cleaning_version": 1,
                },
            ).json()
            rows = api.get(base + f"/traffic/{traffic['id']}/rows").json()["items"]
            rows = [row for row in rows if not row["cleaning_exclusion"]]
            for version, row in enumerate(rows):
                result = api.put(
                    base + f"/traffic/{traffic['id']}/review",
                    json={
                        "expected_version": version,
                        "keyword": row["phrase"]["keyword"],
                        "decision": "keep",
                        "priority": "normal",
                        "reason": "核对",
                        "evidence_note": "人工核对",
                        "reviewer": "运营",
                    },
                )
                assert result.status_code == 200, result.text
            request = {"traffic_id": traffic["id"], "traffic_version": len(rows)}

        artifact = str(uuid4())
        raw_csv = 'ASIN,原始扩展字段\nB012345678,"保持,原样"\nB099999999,未选中商品\n'
        with Repository(engine, tmp_path).connect() as conn:
            competitor_id, text = conn.execute(
                "SELECT id,document FROM competitor_versions ORDER BY version DESC LIMIT 1"
            ).fetchone()
            competitor = json.loads(text)
            competitor["poe_downloads"] = [
                {
                    "id": artifact,
                    "filename": "原始商品.csv",
                    "keyword": "cooling blanket",
                    "sha256": hashlib.sha256(raw_csv.encode("utf-8-sig")).hexdigest(),
                    "source_url": "https://sellercentral.amazon.com/opportunity-explorer/explore/niche/x/product",
                    "rows": 2,
                }
            ]
            conn.execute(
                "UPDATE competitor_versions SET document=? WHERE id=?",
                (
                    json.dumps(competitor),
                    competitor_id,
                ),
            )
            aba_id, raw_text = conn.execute(
                "SELECT id,raw_document FROM aba_reports ORDER BY seq LIMIT 1"
            ).fetchone()
            raw = json.loads(raw_text)
            raw[0]["rows"][0]["unknown-api-field"] = {"zero": 0, "missing": None}
            conn.execute(
                "UPDATE aba_reports SET raw_document=? WHERE id=?",
                (
                    json.dumps(raw),
                    aba_id,
                ),
            )
        archive = tmp_path / "poe_downloads" / base.rsplit("/", 1)[1] / f"{artifact}.csv"
        if archive_available:
            archive.parent.mkdir(parents=True, exist_ok=True)
            archive.write_bytes(raw_csv.encode("utf-8-sig"))

        response = api.post(base + "/library", json=request)
        assert response.status_code == 200, response.text
        draft = response.json()
        assert "workbook_attachments" in draft["snapshots"]
        frozen = draft["snapshots"]["workbook_attachments"]
        attachments = json.loads(frozen)
        cleaning = json.loads(draft["snapshots"]["cleaning"])
        assert attachments["schema_version"] == 1
        assert attachments["sheets"]
        assert all(set(sheet) >= {"name", "csv"} for sheet in attachments["sheets"])
        assert attachments["cleaning_id"] == cleaning["id"]
        root = attachments["aba_roots"][0]
        assert root["source_id"] == aba_id
        assert root["query"] == "blanket"
        assert root["status"] == "available"
        assert root["rows"][0]["unknown-api-field"] == {"zero": 0, "missing": None}
        assert root["report"]["week_end"] == "2026-10-17"

        opportunity = attachments["opportunity"][0]
        assert opportunity["source_id"] == competitor_id
        assert opportunity["query"] == "cooling blanket"
        assert opportunity["artifact"]["id"] == artifact
        if archive_available:
            assert opportunity["status"] == "available"
            assert opportunity["headers"] == ["ASIN", "原始扩展字段"]
            assert opportunity["rows"] == [
                ["B012345678", "保持,原样"],
                ["B099999999", "未选中商品"],
            ]
            archive.unlink()
        else:
            assert opportunity["status"] == "fallback"
            assert opportunity["reason_code"] == "archive_missing"
            assert opportunity["reason"]
            assert opportunity["rows"][0]["asin"] == "B012345678"

        assert {x["source_id"] for x in attachments["aba_expansions"]} == {
            x["id"] for x in cleaning["sources"] if x["provider"] == "aba"
        }
        forward = next(x for x in attachments["aba_expansions"] if x["direction"] == "forward")
        assert forward["report"]["status"] == "succeeded"
        assert forward["report"]["queries"][0]["query"] == "cooling blanket"
        assert forward["report"]["queries"][0]["error_code"] is None
        assert forward["results"][0]["raw_rows"][0]["st-top-asin-1"] == "B0G3GRFJYB"
        assert {x["source_id"] for x in attachments["sellersprite"]} == {
            x["id"] for x in cleaning["sources"] if x["provider"] == "sellersprite"
        }
        reverse = [x for x in attachments["sellersprite"] if x["direction"] == "reverse"]
        assert len(reverse) == (1 if batch else 2)
        assert len(reverse[0]["raw_document"]["rows"]) == 3
        assert reverse[0]["raw_document"]["headers"][0] == "关键词"
        metadata = reverse[0]["report"]["metadata"]
        assert metadata["collection_method"] == (
            "traffic_extension" if batch else "keyword_reverse"
        )
        assert metadata["source_queries"] == (
            ["B012345678", "B087654321"] if batch else [reverse[0]["query"]]
        )
        assert metadata["variant_scope"] == ("all" if batch else None)

        # New records and even changed old raw documents must never affect stored drafts.
        with Repository(engine, tmp_path).connect() as conn:
            for table in (
                "aba_reports",
                "aba_expansions",
                "third_party_imports",
                "core_selections",
                "competitor_versions",
            ):
                row = conn.execute(f"SELECT document FROM {table} LIMIT 1").fetchone()
                if row is not None:
                    changed = json.loads(row[0])
                    changed["warnings"] = ["changed after snapshot"]
                    conn.execute(f"UPDATE {table} SET document=?", (json.dumps(changed),))
            conn.execute("UPDATE aba_reports SET raw_document='[]'")
        restored = api.get(base + f"/library/{draft['id']}").json()
        assert restored["snapshots"]["workbook_attachments"] == frozen
        assert restored["source_sha256"] == draft["source_sha256"]


@pytest.mark.parametrize("case", ["failed", "empty", "missing", "raw_missing", "unselected"])
def test_attachment_snapshot_preserves_unavailable_source_reasons(
    db: str, engine: Engine, tmp_path: Path, case: str
) -> None:
    with ready_library(db, engine, tmp_path) as (api, base, _database_url, request):
        draft = api.post(base + "/library", json=request).json()
        cleaning = CleaningReport.model_validate_json(draft["snapshots"]["cleaning"])
        source = next(
            s for s in cleaning.sources if s.provider == "aba" and s.direction == "forward"
        )
        service = LibraryService(Repository(engine, tmp_path), cast(TrafficService, None))
        with Repository(engine, tmp_path).connect() as conn:
            document, results = conn.execute(
                "SELECT document,results FROM aba_expansions WHERE id=?", (str(source.id),)
            ).fetchone()
            report, raw = json.loads(document), json.loads(results)
            if case == "failed":
                report["status"] = "partial"
                report["queries"][0].update(error_code="ABA_TIMEOUT", error="请求超时，保留已有页")
            elif case == "empty":
                raw[0]["rows"], raw[0]["raw_rows"] = [], []
            elif case == "raw_missing":
                raw[0].pop("raw_rows")
            elif case == "missing":
                conn.execute("DELETE FROM aba_expansions WHERE id=?", (str(source.id),))
            else:
                cleaning.sources = [s for s in cleaning.sources if s.id != source.id]
            conn.execute(
                "UPDATE aba_expansions SET document=?,results=? WHERE id=?",
                (json.dumps(report), json.dumps(raw), str(source.id)),
            )
            attachments = json.loads(
                service._attachment_snapshot(  # pyright: ignore[reportPrivateUsage]
                    conn, UUID(base.rsplit("/", 1)[1]), cleaning, draft["snapshots"]
                )
            )
        item = next(x for x in attachments["aba_expansions"] if x["direction"] == "forward")
        expected = {
            "failed": ("failed", "query_failed"),
            "empty": ("empty", "no_rows"),
            "missing": ("missing", "source_missing"),
            "raw_missing": ("missing", "raw_rows_missing"),
            "unselected": ("missing", "source_not_selected"),
        }
        assert (item["status"], item["reason_code"]) == expected[case]
        assert item["reason"]
        if case == "failed":
            assert "ABA_TIMEOUT" in item["reason"] and "请求超时" in item["reason"]
            assert len(item["results"][0]["raw_rows"]) == 1


@pytest.mark.parametrize(
    "delimiter,encoding,csv_text,header,value",
    [
        (";", "utf-8-sig", 'ASIN;原始字段\r\nB012345678;"原样,保留"\r\n', "原始字段", "原样,保留"),
        ("\t", "utf-16", 'ASIN\t原始字段\r\nB012345678\t"原样,保留"\r\n', "原始字段", "原样,保留"),
        (
            ",",
            "utf-8-sig",
            "ASIN, Product Title\nB012345678,  original value\n",
            " Product Title",
            "  original value",
        ),
    ],
)
def test_opportunity_snapshot_keeps_amazon_preamble_and_original_headers(
    db: str,
    engine: Engine,
    tmp_path: Path,
    delimiter: str,
    encoding: str,
    csv_text: str,
    header: str,
    value: str,
) -> None:
    with ready_library(db, engine, tmp_path) as (api, base, _database_url, request):
        draft = api.post(base + "/library", json=request).json()
        cleaning = CleaningReport.model_validate_json(draft["snapshots"]["cleaning"])
        snapshots = draft["snapshots"]
        competitor = json.loads(snapshots["competitor_versions"])
        artifact = str(uuid4())
        preamble = "Niche Name: cooling blanket\r\nLast Updated: 2026-10-17\r\n\r\n"
        content = (preamble + csv_text).encode(encoding)
        competitor["poe_downloads"] = [
            {
                "id": artifact,
                "keyword": "cooling blanket",
                "filename": "原件.csv",
                "rows": 1,
                "sha256": hashlib.sha256(content).hexdigest(),
                "source_url": "",
            }
        ]
        snapshots["competitor_versions"] = json.dumps(competitor)
        archive = tmp_path / "poe_downloads" / base.rsplit("/", 1)[1] / f"{artifact}.csv"
        archive.parent.mkdir(parents=True)
        archive.write_bytes(content)
        service = LibraryService(Repository(engine, tmp_path), cast(TrafficService, None))
        with Repository(engine, tmp_path).connect() as conn:
            frozen = json.loads(
                service._attachment_snapshot(  # pyright: ignore[reportPrivateUsage]
                    conn,
                    UUID(base.rsplit("/", 1)[1]),
                    cleaning,
                    snapshots,
                )
            )
        opportunity = frozen["opportunity"][0]
        assert opportunity["headers"] == ["ASIN", header]
        assert opportunity["rows"] == [["B012345678", value]]
        assert opportunity["preamble"] == preamble
        assert opportunity["delimiter"] == delimiter
