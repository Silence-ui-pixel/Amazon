"""第五步：真实导入、字段映射、来源与完整性约束。"""

import io
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from test_aba_expansion import prepare

from app.adapters.third_party import MAX_BYTES, map_rows, read_table, suggest
from app.db.repository import Repository
from app.domain.errors import DomainError
from app.main import create_app


def test_batch_source_metadata_keeps_covered_asins_and_legacy_defaults(db: str, engine: Engine, ) -> None:
    from datetime import UTC, datetime

    from app.domain.third_party import ImportMetadata

    legacy = ImportMetadata(
        direction="reverse",
        query="B0BGP8RBWR",
        marketplace="US",
        period_kind="last_30_days",
        collected_at=datetime.now(UTC),
    )
    assert legacy.collection_method == "keyword_reverse"
    assert legacy.source_queries == ["B0BGP8RBWR"]
    assert legacy.variant_scope is None

    batch = ImportMetadata(
        direction="reverse",
        query="B0BGP8RBWR,B01G643EU6",
        marketplace="US",
        period_kind="last_30_days",
        collected_at=datetime.now(UTC),
        collection_method="traffic_extension",
        source_queries=["B0BGP8RBWR", "B01G643EU6"],
        variant_scope="all",
    )
    assert batch.source_queries == ["B0BGP8RBWR", "B01G643EU6"]


CSV = '关键词,关键词翻译,月搜索量,ABA月排名,#1 点击共享,#1转化共享\n Cooling   Blanket ,凉毯,"1,200",20,10%,6%\ncooling blanket,凉毯,1200,20,0%,0%\n=1+1,,0,,,\n'.encode(
    "utf-8-sig"
)
MAPPING = {
    "keyword": "关键词",
    "translation": "关键词翻译",
    "monthly_searches": "月搜索量",
    "monthly_rank": "ABA月排名",
    "top1_click_share": "#1 点击共享",
    "top1_conversion_share": "#1转化共享",
}


def test_parse_map_preserves_evidence_units_and_unknown(db: str, engine: Engine, ) -> None:
    table = read_table("词表.csv", CSV)
    rows = map_rows(table, MAPPING, "percent")
    assert len(rows) == 3
    assert rows[0].keyword == "Cooling   Blanket"
    assert rows[0].normalized_keyword == "cooling blanket"
    assert rows[0].monthly_searches == 1200
    assert str(rows[0].top1_click_share) == "0.1"
    assert rows[1].top1_click_share == 0
    assert rows[2].top1_click_share is None
    assert rows[0].raw["关键词"] == " Cooling   Blanket "
    assert rows[0].row_number == 2


def test_xlsx_inline_shared_strings_and_formula_rejection(db: str, engine: Engine, ) -> None:
    stream = io.BytesIO()
    with ZipFile(stream, "w") as z:
        z.writestr(
            "xl/workbook.xml",
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="词表" sheetId="1" r:id="rId1"/></sheets></workbook>',
        )
        z.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
        )
        z.writestr("xl/sharedStrings.xml", "<sst><si><t>关键词</t></si></sst>")
        z.writestr(
            "xl/worksheets/sheet1.xml",
            '<worksheet><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="inlineStr"><is><t>月搜索量</t></is></c></row><row r="2"><c r="A2" t="inlineStr"><is><t>blanket</t></is></c><c r="B2"><f>1+1</f><v>2</v></c></row></sheetData></worksheet>',
        )
    table = read_table("词表.xlsx", stream.getvalue())
    assert table.headers == ["关键词", "月搜索量"]
    with pytest.raises(DomainError, match="公式"):
        map_rows(table, {"keyword": "关键词", "monthly_searches": "月搜索量"}, "fraction")


@pytest.mark.parametrize(
    "text",
    [
        "关键词,关键词\na,b",
        "keyword,月搜索量\na,NaN",
        "keyword,月搜索量\na,-1",
        "keyword,月搜索量\na,1.2",
    ],
)
def test_invalid_tables_reject_entire_batch(db: str, engine: Engine, text: str) -> None:
    with pytest.raises(DomainError):
        table = read_table("x.csv", text.encode())
        map_rows(table, {"keyword": "keyword", "monthly_searches": "月搜索量"}, "fraction")


def preview(
    api: TestClient,
    url: str,
    query: str,
    direction: str = "forward",
    complete: bool = True,
    *,
    recent: bool = False,
    metadata_extra: Mapping[str, object] | None = None,
) -> dict[str, object]:

    result = api.post(
        url + "/preview",
        files={"file": ("seller.csv", CSV, "text/csv")},
        data={
            "metadata": json.dumps(
                {
                    "direction": direction,
                    "query": query,
                    "marketplace": "US",
                    "period_kind": "last_30_days" if recent else "date_range",
                    "period_start": None if recent else "2026-10-01",
                    "period_end": None if recent else "2026-10-31",
                    "collected_at": datetime.now(UTC).isoformat() if recent else None,
                    "source_total": 3,
                    "export_complete": complete,
                    **(metadata_extra or {}),
                }
            )
        },
    )
    assert result.status_code == 200, result.text
    return result.json()


def test_batch_report_covers_each_source_asin(db: str, engine: Engine, tmp_path: Path) -> None:
    base = prepare(db, engine, tmp_path)
    url = base + "/third-party"
    with TestClient(create_app(db, storage_root=tmp_path)) as api:
        batch = preview(
            api,
            url,
            "B012345678,B087654321",
            "reverse",
            metadata_extra={
                "collection_method": "traffic_extension",
                "source_queries": ["B012345678", "B087654321"],
                "variant_scope": "all",
            },
        )
        confirmed = api.post(
            url + f"/{batch['id']}/confirm",
            json={"mapping": MAPPING, "share_unit": "percent"},
        )
        assert confirmed.status_code == 200, confirmed.text
        workspace = api.get(url).json()
        assert workspace["covered_reverse"] == 2


def test_import_confirm_restore_coverage_export_and_stale(db: str, engine: Engine, tmp_path: Path) -> None:
    base = prepare(db, engine, tmp_path)
    url = base + "/third-party"
    with TestClient(create_app(db, storage_root=tmp_path)) as api:
        draft = preview(api, url, "cooling blanket")
        assert draft["status"] == "preview"
        assert preview(api, url, "cooling blanket")["id"] == draft["id"]
        assert api.get(url).json()["complete"] is False
        response = api.post(
            url + f"/{draft['id']}/confirm", json={"mapping": MAPPING, "share_unit": "percent"}
        )
        assert response.status_code == 200, response.text
        report = response.json()
        assert report["unique_rows"] == 2 and report["imported_rows"] == 3
        assert report["complete"] is True
        assert (
            api.post(
                url + f"/{draft['id']}/confirm", json={"mapping": MAPPING, "share_unit": "percent"}
            ).json()["id"]
            == report["id"]
        )
        assert (
            api.post(
                url + f"/{draft['id']}/confirm",
                json={"mapping": {"keyword": "关键词"}, "share_unit": "fraction"},
            ).status_code
            == 409
        )
        for asin in ["B012345678", "B087654321"]:
            d = preview(api, url, asin, "reverse")
            assert (
                api.post(
                    url + f"/{d['id']}/confirm", json={"mapping": MAPPING, "share_unit": "percent"}
                ).status_code
                == 200
            )
        assert api.get(url).json()["complete"] is True
        page = api.get(url + f"/{report['id']}/rows?limit=1").json()
        assert page["total"] == 3 and len(page["items"]) == 1
        assert page["items"][0]["raw"]["月搜索量"] == "1,200"
        download = api.get(url + f"/{report['id']}/export")
        assert download.status_code == 200 and "'=1+1" in download.text
        assert "cooling blanket" in download.text and "2026-10-01" in download.text
        newer = preview(api, url, "cooling blanket", complete=False)
        api.post(
            url + f"/{newer['id']}/confirm", json={"mapping": MAPPING, "share_unit": "percent"}
        )
        assert api.get(url).json()["complete"] is False
    with TestClient(create_app(db, storage_root=tmp_path)) as api:
        assert len(api.get(url).json()["reports"]) == 4
        with Repository(engine, tmp_path).connect() as conn:
            conn.execute("DELETE FROM root_selections")
        assert all(r["stale"] for r in api.get(url).json()["reports"])
        assert preview_stale(api, url, report["id"]) == 409


def preview_stale(api: TestClient, url: str, report_id: str) -> int:
    return api.post(
        url + f"/{report_id}/confirm", json={"mapping": MAPPING, "share_unit": "percent"}
    ).status_code


def test_import_scope_and_input_validation(db: str, engine: Engine, tmp_path: Path) -> None:
    base = prepare(db, engine, tmp_path)
    url = base + "/third-party"
    with TestClient(create_app(db, storage_root=tmp_path)) as api:
        import json

        metadata = {
            "direction": "forward",
            "query": "unconfirmed",
            "marketplace": "US",
            "period_start": "2026-10-01",
            "period_end": "2026-10-31",
            "export_complete": True,
            "source_total": 3,
        }

        def upload() -> int:
            return api.post(
                url + "/preview",
                data={"metadata": json.dumps(metadata)},
                files={"file": ("x.csv", CSV)},
            ).status_code

        assert upload() == 422
        metadata["query"] = "cooling blanket"
        metadata["marketplace"] = "DE"
        assert upload() == 422
        metadata["marketplace"] = "US"
        metadata["source_total"] = 1
        assert upload() == 422
        d = preview(api, url, "cooling blanket")
        p = api.post("/api/v1/projects", json={"name": "other", "marketplace": "US"}).json()["id"]
        other = url.replace(base.split("/")[4], p)
        assert api.get(other + f"/{d['id']}/rows").status_code == 404


def test_latest_confirmation_controls_period_and_new_batch(db: str, engine: Engine, tmp_path: Path) -> None:

    base = prepare(db, engine, tmp_path)
    url = base + "/third-party"
    with TestClient(create_app(db, storage_root=tmp_path)) as api:
        old = preview(api, url, "cooling blanket", complete=False)
        newer = preview(api, url, "cooling blanket")
        for draft in [newer, old]:
            assert (
                api.post(
                    url + f"/{draft['id']}/confirm",
                    json={"mapping": MAPPING, "share_unit": "percent"},
                ).status_code
                == 200
            )
        assert api.get(url).json()["covered_forward"] == 0
        meta = json.loads(json.dumps(newer["metadata"]))
        meta["batch_key"] = "explicit-new-batch"
        response = api.post(
            url + "/preview",
            data={"metadata": json.dumps(meta)},
            files={"file": ("x.csv", CSV)},
        )
        assert response.status_code == 200
        assert response.json()["id"] != newer["id"]


def test_export_escapes_formula_headers(db: str, engine: Engine, tmp_path: Path) -> None:

    base = prepare(db, engine, tmp_path)
    url = base + "/third-party"
    with TestClient(create_app(db, storage_root=tmp_path)) as api:
        draft = preview(api, url, "cooling blanket")
        meta = json.loads(json.dumps(draft["metadata"]))
        meta["source_total"] = 1
        draft = api.post(
            url + "/preview",
            data={"metadata": json.dumps(meta)},
            files={"file": ("x.csv", "关键词,=1+1\na,b".encode())},
        ).json()
        response = api.post(
            url + f"/{draft['id']}/confirm",
            json={"mapping": {"keyword": "关键词"}, "share_unit": "fraction"},
        )
        assert response.status_code == 200, response.text
        assert "'=1+1" in api.get(url + f"/{draft['id']}/export").text.splitlines()[0]


def test_repeated_xlsx_sheet_references_stop_before_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.adapters import third_party

    stream = io.BytesIO()
    with ZipFile(stream, "w") as archive:
        sheets = "".join(f'<sheet name="s{i}" r:id="r1"/>' for i in range(51))
        archive.writestr(
            "xl/workbook.xml", f'<workbook xmlns:r="r"><sheets>{sheets}</sheets></workbook>'
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships><Relationship Id="r1" Target="worksheets/sheet1.xml"/></Relationships>',
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            '<worksheet><row r="1"><c r="A1" t="inlineStr"><is><t>keyword</t></is></c></row><row r="2"><c r="A2" t="inlineStr"><is><t>a</t></is></c></row></worksheet>',
        )
    parsed: list[bytes] = []
    original = third_party.ET.fromstring

    def tracked(payload: bytes) -> third_party.ET.Element:
        parsed.append(payload)
        return original(payload)

    monkeypatch.setattr(third_party.ET, "fromstring", tracked)
    with pytest.raises(DomainError):
        read_table("x.xlsx", stream.getvalue())
    assert len(parsed) <= 3


@pytest.mark.parametrize(
    "value,unit",
    [("10%", "fraction"), ("101", "percent"), ("1.01", "fraction"), ("=1+1", "percent")],
)
def test_share_units_and_ranges_reject(db: str, engine: Engine, value: str, unit: str) -> None:
    from typing import Literal, cast

    table = read_table("x.csv", f"keyword,share\na,{value}".encode())
    with pytest.raises(DomainError):
        map_rows(
            table,
            {"keyword": "keyword", "top1_click_share": "share"},
            cast(Literal["fraction", "percent"], unit),
        )


def test_encoding_aliases_and_file_boundaries(db: str, engine: Engine, ) -> None:
    assert read_table("x.tsv", "关键词\t月搜索量\n凉毯\t0".encode("gb18030")).rows == [
        ["凉毯", "0"]
    ]
    assert "keyword" not in suggest(["关键词", "keyword"])
    for filename, data in [
        ("x.xls", CSV),
        ("x.csv", b"keyword\n"),
        ("x.csv", b"a" * (MAX_BYTES + 1)),
        ("x.xlsx", b"broken"),
    ]:
        with pytest.raises(DomainError):
            read_table(filename, data)


def test_original_file_retained_and_bid_currency_required(db: str, engine: Engine, tmp_path: Path) -> None:
    base = prepare(db, engine, tmp_path)
    url = base + "/third-party"
    with TestClient(create_app(db, storage_root=tmp_path)) as api:
        draft = preview(api, url, "cooling blanket")
        with Repository(engine, tmp_path).connect() as conn:
            assert (
                conn.execute(
                    "SELECT content FROM third_party_import_files WHERE report_id=?", (draft["id"],)
                ).fetchone()[0]
                == CSV
            )
        response = api.post(
            url + f"/{draft['id']}/confirm",
            json={"mapping": {"keyword": "关键词", "bid": "月搜索量"}, "share_unit": "percent"},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "IMPORT_CURRENCY_REQUIRED"
        meta = json.loads(json.dumps(draft["metadata"]))
        meta["currency"] = "USD"
        draft = api.post(
            url + "/preview", data={"metadata": json.dumps(meta)}, files={"file": ("x.csv", CSV)}
        ).json()
        response = api.post(
            url + f"/{draft['id']}/confirm",
            json={"mapping": {"keyword": "关键词", "bid": "月搜索量"}, "share_unit": "percent"},
        )
        assert response.status_code == 200
        assert response.json()["metadata"]["currency"] == "USD"
