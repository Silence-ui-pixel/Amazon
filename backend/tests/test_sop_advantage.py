"""以课程 SOP 的购买、垄断和相关性证据输出竞争优势。"""

import io
from decimal import Decimal
from pathlib import Path
from xml.etree import ElementTree
from zipfile import ZipFile

import pytest
from sqlalchemy import Engine
from test_library import ready_library

from app.domain.traffic import MetricInput, RuleSet, TrafficRow, evaluate, sop_advice


def test_purchase_sixty_percent_and_partial_monopoly() -> None:
    result = evaluate(
        MetricInput(rank=150000, click=Decimal("0.5"), conversion=Decimal("0.3")), RuleSet()
    )
    assert result.purchase is False
    assert result.monopoly is True
    assert result.conversion_sum3 is None
    assert result.first_click_share == Decimal("0.5")
    assert result.first_conversion_share == Decimal("0.3")
    assert result.deep_analysis is True
    # 旧规则快照仍按原有严格边界解释。
    legacy = evaluate(
        MetricInput(click=Decimal("0.5"), conversion=Decimal("0.3")), RuleSet(id="sop-traffic-v1")
    )
    assert legacy.purchase is False


@pytest.mark.parametrize(
    "purchase,monopoly,related,expected",
    [
        (True, False, "high", "先投"),
        (True, True, "high", "后投（垄断词）"),
        (False, False, "high", "后投（购买比不足）"),
        (True, None, "high", "待补ABA指标"),
        (True, False, "unknown", "待核对相关性"),
        (True, False, "low", "低相关，复核后决定"),
    ],
)
def test_sop_advice_preserves_unknown_and_delays_monopoly(
    purchase: bool | None, monopoly: bool | None, related: str, expected: str
) -> None:
    row = TrafficRow.model_validate(
        {
            "phrase": {"keyword": "gate wheel", "evidence": []},
            "metrics": {"purchase": purchase, "monopoly": monopoly},
            "review": {
                "version": 1,
                "created_at": "2026-10-27T00:00:00Z",
                "relevance": related,
                "data": {
                    "expected_version": 0,
                    "keyword": "gate wheel",
                    "decision": "keep",
                    "priority": "normal",
                    "reason": "核对",
                    "evidence_note": "待补证据",
                    "reviewer": "运营",
                },
            },
        }
    )
    assert sop_advice(row) == expected
    assert row.review is not None and row.review.data.decision == "keep"


def test_library_and_excel_include_sop_evidence_and_unknown_labels(
    db: str, engine: Engine, tmp_path: Path
) -> None:
    with ready_library(db, engine, tmp_path) as (api, base, _database_url, request):
        draft = api.post(base + "/library", json=request).json()
        url = base + f"/library/{draft['id']}"
        table = api.get(url + "/template").json()
        row = dict(zip(table["headers"], table["rows"][0], strict=True))
        assert row["相关性"] == "待核对"
        assert row["垄断性"] == "待补数据"
        assert row["购买性"] == "否"
        assert row["SOP投放建议"] == "待核对相关性"
        assert row["购买比"] == "40%"
        assert row["款式相近"] == "待核对"
        assert row["统计范围"] == "扩展词（排名超过15万）"
        assert any("60%" in r for r in table["rule_notes"])
        excel = api.get(url + "/template.xlsx")
        with ZipFile(io.BytesIO(excel.content)) as archive:
            sheet = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
            namespace = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
            auto_filter = sheet.find("s:autoFilter", namespace)
            assert auto_filter is not None
            assert auto_filter.attrib["ref"].startswith("A11:AG")
            assert "SOP投放建议" in archive.read("xl/worksheets/sheet1.xml").decode()
            workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
            names = [s.attrib["name"] for s in workbook.findall("s:sheets/s:sheet", namespace)]
            assert names[0] == "关键词库"
            assert "SOP规则说明" not in names
            assert names[1].startswith("ABA核心大词-")
