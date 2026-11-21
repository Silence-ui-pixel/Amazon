"""最终发布必须来自真实审核结果并保留不可变历史。"""

import csv
import io
import json
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from zipfile import ZipFile

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from test_aba import auth_file, row
from test_aba_expansion import prepare, wait
from test_third_party import MAPPING, preview

from app.adapters.aba import AbaClient
from app.db.repository import Repository
from app.main import create_app


@pytest.mark.parametrize(
    ("rank", "tier"),
    [
        (None, "unranked"),
        (0, "unranked"),
        (1, "L1"),
        (30000, "L1"),
        (30001, "L2"),
        (150000, "L2"),
        (150001, "L3"),
        (300000, "L3"),
        (300001, "L4"),
    ],
)
def test_tier_boundaries(db: str, engine: Engine, rank: int | None, tier: str) -> None:
    from app.domain.library import LibraryRules, tier_of

    assert tier_of(rank, LibraryRules()) == tier


def test_tier_rules_are_configurable_and_versioned(db: str, engine: Engine, ) -> None:
    from pydantic import ValidationError

    from app.domain.library import LibraryRules, tier_of

    rules = LibraryRules(l1_max=10, l2_max=20, l3_max=30)
    assert rules.id == "sop-library-v1"
    assert [tier_of(rank, rules) for rank in (0, 10, 11, 20, 21, 30, 31, None)] == [
        "unranked",
        "L1",
        "L2",
        "L2",
        "L3",
        "L3",
        "L4",
        "unranked",
    ]
    with pytest.raises(ValidationError):
        LibraryRules(l1_max=20, l2_max=10)


@contextmanager
def ready_library(
    db: str,
    engine: Engine,
    tmp_path: Path,
    roots: list[dict[str, str]] | None = None,
) -> Generator[tuple[TestClient, str, str, dict[str, object]]]:
    base = prepare(db, engine, tmp_path)
    aba = AbaClient(
        auth_file(tmp_path),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "reportsV2": [
                        {"totalItems": 1, "rows": [row("portable cooling blankets", 200001)]}
                    ]
                },
            )
        ),
    )
    with TestClient(create_app(db, storage_root=tmp_path, aba_client=aba)) as api:
        for direction in ("forward", "reverse"):
            task = api.post(
                base + "/aba-expansion",
                json={"direction": direction, "week_end": "2026-10-17", "max_rows": 100},
            ).json()
            assert wait(api, base + "/aba-expansion", task["id"])["status"] == "succeeded"
        for direction, query in [
            ("forward", "cooling blanket"),
            ("reverse", "B012345678"),
            ("reverse", "B087654321"),
        ]:
            imported = preview(api, base + "/third-party", query, direction, recent=True)
            assert (
                api.post(
                    base + f"/third-party/{imported['id']}/confirm",
                    json={"mapping": MAPPING, "share_unit": "percent"},
                ).status_code
                == 200
            )
        clean = api.post(
            base + "/cleaning",
            json={
                "aba_week_end": "2026-10-17",
                "seller_period": "last_30_days",
                "text_only_acknowledged": True,
            },
        ).json()
        assert (
            api.put(
                base + f"/cleaning/{clean['id']}/review",
                json={
                    "expected_version": 0,
                    "roots": roots
                    or [{"term": "portable", "category": "preference"}],
                    "exclusions": [{"keyword": "=1+1", "reason": "噪声"}],
                    "reason": "分类核对",
                },
            ).status_code
            == 200
        )
        traffic = api.post(
            base + "/traffic", json={"cleaning_id": clean["id"], "cleaning_version": 1}
        ).json()
        for version, keyword in enumerate(["cooling blanket", "portable cooling blankets"]):
            review = {
                "expected_version": version,
                "keyword": keyword,
                "decision": "keep",
                "priority": "normal",
                "reason": "确认入库",
                "evidence_note": "人工核对",
                "reviewer": "本地运营",
            }
            response = api.put(base + f"/traffic/{traffic['id']}/review", json=review)
            assert response.status_code == 200, response.text
        yield api, base, db, {"traffic_id": traffic["id"], "traffic_version": 2}


def test_confirmed_roots_in_template_excel_and_legacy_draft(db: str, engine: Engine, tmp_path: Path) -> None:
    with ready_library(db, engine, tmp_path) as (api, base, database_url, request):
        draft = api.post(base + "/library", json=request).json()
        url = base + f"/library/{draft['id']}"
        root_source = json.loads(draft["snapshots"]["root_source"])
        expected = root_source["roots_snapshot"]
        assert expected
        assert api.get(url + "/template").json()["summary"][0] == ["词根", *expected]
        excel = api.get(url + "/template.xlsx")
        with ZipFile(io.BytesIO(excel.content)) as archive:
            assert expected[0] in archive.read("xl/worksheets/sheet1.xml").decode()
        # Old drafts have no root snapshot: resolve only the pinned ABA report,
        # even if the current selection has since been removed or changed.
        del draft["snapshots"]["root_source"]
        with Repository(engine, tmp_path).connect() as conn:
            conn.execute(
                "UPDATE keyword_library_drafts SET document=? WHERE id=?",
                (json.dumps(draft), draft["id"]),
            )
            conn.execute("DELETE FROM root_selections WHERE run_id=?", (draft["run_id"],))
        assert api.get(url + "/template").json()["summary"][0] == ["词根", *expected]
        with Repository(engine, tmp_path).connect() as conn:
            stored = json.loads(
                conn.execute(
                    "SELECT document FROM keyword_library_drafts WHERE id=?", (draft["id"],)
                ).fetchone()[0]
            )
        assert "root_source" not in stored["snapshots"]


def test_publication_snapshot_conflict_history_and_export(db: str, engine: Engine, tmp_path: Path) -> None:
    with ready_library(db, engine, tmp_path) as (api, base, database_url, request):
        url = base + "/library"
        response = api.post(url, json=request)
        assert response.status_code == 200, response.text
        draft = response.json()
        key = draft["id"]
        assert api.post(url, json=request).json()["id"] == key
        assert [(e["keyword"], e["tier"]) for e in draft["entries"]] == [
            ("portable cooling blankets", "L3"),
            ("cooling blanket", "unranked"),
        ]
        assert draft["entries"][0]["roots"]["preference"] == ["portable"]
        assert draft["suggestions"] == [{"tier": "L3", "asin": "B0G3GRFJYB", "keyword_count": 1}]
        edit = {
            "expected_version": 0,
            "keyword": "cooling blanket",
            "priority": "high",
            "usage": "listing",
            "auxiliary_asins": ["b012345678"],
            "reason": "用于标题",
            "operator": "运营",
        }
        saved = api.put(url + f"/{key}/entry", json=edit)
        assert saved.status_code == 200, saved.text
        assert api.put(url + f"/{key}/entry", json=edit).status_code == 409
        publish = {
            "expected_version": 1,
            "publisher": "运营",
            "reason": "本月确认",
            "confirmed": True,
            "exception_reviews": [
                {"keyword": e["keyword"], "reason": "人工核对缺失证据后保留"}
                for e in draft["entries"]
                if e["issues"]
            ],
        }
        first = api.post(url + f"/{key}/publish", json=publish)
        assert first.status_code == 200, first.text
        snapshot = first.json()
        assert snapshot["publication_version"] == 1
        assert api.post(url + f"/{key}/publish", json=publish).json() == snapshot
        assert (
            api.put(url + f"/{key}/entry", json={**edit, "expected_version": 1}).status_code == 409
        )
        revision = api.post(url, json={**request, "revision_of": key})
        assert revision.status_code == 200, revision.text
        revised = revision.json()
        assert revised["id"] != key and revised["publication_version"] is None
        assert revised["entries"] == snapshot["entries"]
        assert api.post(url, json={**request, "revision_of": key}).json()["id"] == revised["id"]
        export = api.get(url + f"/{key}/export")
        records = list(csv.DictReader(io.StringIO(export.text.lstrip("\ufeff"))))
        assert len(records) == 2
        assert records[1]["CTR"] == "" and records[1]["ACOS"] == ""
        assert records[1]["辅助ASIN"] == "B012345678"
        assert "sop-library-v1" in export.text and "2026-10-17" in export.text
        # A new human decision invalidates drafts but never mutates published content.
        assert (
            api.put(
                base + f"/traffic/{request['traffic_id']}/review",
                json={
                    "expected_version": 2,
                    "keyword": "cooling blanket",
                    "decision": "exclude",
                    "priority": "low",
                    "reason": "重新判断",
                    "evidence_note": "复核",
                    "reviewer": "运营",
                },
            ).status_code
            == 200
        )
        assert api.get(url + f"/{key}").json()["entries"] == snapshot["entries"]
        assert api.get(url + f"/{key}").json()["stale"] is True
        second = api.post(url, json={**request, "traffic_version": 3}).json()
        assert len(second["entries"]) == 1
        assert (
            api.post(
                url + f"/{second['id']}/publish",
                json={
                    **publish,
                    "expected_version": 0,
                    "exception_reviews": [
                        {"keyword": e["keyword"], "reason": "重新核对"}
                        for e in second["entries"]
                        if e["issues"]
                    ],
                },
            ).json()["publication_version"]
            == 2
        )
        other = api.post("/api/v1/projects", json={"name": "other", "marketplace": "US"}).json()[
            "id"
        ]
        assert api.get(url.replace(base.split("/")[4], other) + f"/{key}").status_code == 404
    with TestClient(create_app(db, storage_root=tmp_path)) as api:
        assert len(api.get(url).json()) == 3
        assert api.get(url + f"/{key}").json()["entries"] == snapshot["entries"]


def test_stale_draft_and_incomplete_rejected(db: str, engine: Engine, tmp_path: Path) -> None:
    with ready_library(db, engine, tmp_path) as (api, base, _database_url, request):
        url = base + "/library"
        assert api.post(url, json={**request, "traffic_version": 1}).status_code == 409
        draft = api.post(url, json=request).json()
        assert (
            api.put(
                base + f"/traffic/{request['traffic_id']}/review",
                json={
                    "expected_version": 2,
                    "keyword": "cooling blanket",
                    "decision": "defer",
                    "priority": "low",
                    "reason": "待核对",
                    "evidence_note": "缺证据",
                    "reviewer": "运营",
                },
            ).status_code
            == 200
        )
        assert (
            api.post(
                url + f"/{draft['id']}/publish",
                json={
                    "expected_version": 0,
                    "publisher": "运营",
                    "reason": "确认",
                    "confirmed": True,
                },
            ).status_code
            == 409
        )
        assert api.post(url, json={**request, "traffic_version": 3}).status_code == 409


def test_concurrent_publication_retries_are_idempotent(db: str, engine: Engine, tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    with ready_library(db, engine, tmp_path) as (api, base, _database_url, data):
        draft = api.post(base + "/library", json=data).json()
        publish: dict[str, object] = {
            "expected_version": 0,
            "publisher": "运营",
            "reason": "确认",
            "confirmed": True,
        }
        publish["exception_reviews"] = [
            {"keyword": e["keyword"], "reason": "人工核对"} for e in draft["entries"] if e["issues"]
        ]

        def send(_: int) -> int:
            response = api.post(base + f"/library/{draft['id']}/publish", json=publish)
            assert response.status_code == 200, response.text
            return int(response.json()["publication_version"])

        with ThreadPoolExecutor(max_workers=4) as pool:
            versions = list(pool.map(send, range(4)))
        assert versions == [1] * 4
        revision = api.post(base + "/library", json={**data, "revision_of": draft["id"]}).json()
        response = api.post(base + f"/library/{revision['id']}/publish", json=publish)
        assert response.status_code == 200, response.text
        assert response.json()["publication_version"] == 2


def test_library_create_keeps_unclassified_roots(db: str, engine: Engine, tmp_path: Path) -> None:
    """清洗模型给出 unclassified 词根时，建库不能因缺少分类桶而崩溃。"""
    roots = [
        {"term": "portable", "category": "preference"},
        {"term": "cooling", "category": "unclassified"},
    ]
    with ready_library(db, engine, tmp_path, roots=roots) as (api, base, _database_url, data):
        draft = api.post(base + "/library", json=data)
        assert draft.status_code == 200, draft.text
        entries = {entry["keyword"]: entry for entry in draft.json()["entries"]}
        assert entries["cooling blanket"]["roots"]["unclassified"] == ["cooling"]
        assert entries["portable cooling blankets"]["roots"]["preference"] == ["portable"]
