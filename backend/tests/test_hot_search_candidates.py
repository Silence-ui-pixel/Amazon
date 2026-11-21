"""真实 ABA 服务与候选门控；仅隔离 Seller Central HTTP 传输。"""

import json
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from test_aba import auth_file, row, seed_run

from app.adapters.aba import AbaClient
from app.db.repository import Repository
from app.domain.aba import AbaReport, AbaRequest
from app.domain.roots import RootCandidate, RootCandidates, RootSelection
from app.main import create_app
from app.services.aba import AbaService
from app.services.core_terms import CoreService


def seed_product_terms(
    client: TestClient, engine: Engine, tmp_path: Path
) -> tuple[str, str]:
    project, run, _ = seed_run(client, engine, tmp_path)
    candidates = RootCandidates(
        project_id=UUID(project),
        run_id=UUID(run),
        model="test",
        marketplace="US",
        language="en",
        created_at=datetime.now(UTC),
        warnings=[],
        rule_version="product-term-v1",
        candidates=[
            RootCandidate(id=uuid4(), text=t, translation="商品词", reason="商品类型")
            for t in ["shoe rack", "shoe organizer"]
        ],
    )
    selection = RootSelection(
        project_id=UUID(project),
        run_id=UUID(run),
        candidate_ids=[c.id for c in candidates.candidates],
        confirmed_at=datetime.now(UTC),
    )
    with Repository(engine, tmp_path).connect() as conn:
        conn.execute(
            "UPDATE root_candidates SET document=? WHERE run_id=?",
            (candidates.model_dump_json(), run),
        )
        conn.execute(
            "UPDATE root_selections SET document=? WHERE run_id=?",
            (selection.model_dump_json(), run),
        )
    return project, run


def frozen_date(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    # 固定服务时钟，抓住周六当天被误当完整周、跨周日计算错误。
    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            return datetime.fromisoformat(value).replace(tzinfo=UTC)

    monkeypatch.setattr("app.services.aba.datetime", Clock)


@pytest.mark.parametrize(
    ("today", "week"),
    [
        ("2026-10-29T12:00:00", "2026-10-24"),
        ("2026-10-24T12:00:00", "2026-10-17"),
        ("2026-10-25T00:00:00", "2026-10-24"),
    ],
)
def test_product_terms_query_separately_using_last_completed_week(
    db: str,
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    today: str,
    week: str,
) -> None:
    # 抓住商品词拼接、退回 ASIN 搜索，以及缺失新模式快照。
    frozen_date(monkeypatch, today)
    requests: list[list[dict[str, object]]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body["filterSelections"])
        return httpx.Response(
            200, json={"reportsV2": [{"totalItems": 1, "rows": [row("storage")]}]}
        )

    provider = AbaClient(auth_file(tmp_path), transport=httpx.MockTransport(handle))
    with TestClient(create_app(db, storage_root=tmp_path, aba_client=provider)) as client:
        project, run = seed_product_terms(client, engine, tmp_path)
        url = f"/api/v1/projects/{project}/pipeline-runs/{run}/aba-reports"
        response = client.post(url, json={"query_mode": "product_term", "week_end": week})
        assert response.status_code == 200, response.text
        saved = response.json()
        assert saved["week_end"] == week
        assert saved["roots_snapshot"] == ["shoe rack", "shoe organizer"]
        assert saved["query_terms_snapshot"] == ["shoe rack", "shoe organizer"]
        assert saved["rule_version"] == "hot-search-core-v1"
        assert [q["query_mode"] for q in saved["reports"]] == ["product_term", "product_term"]
        assert requests == [
            [
                {"id": "reporting-range", "value": "weekly", "valueType": None},
                {"id": "weekly-week", "value": week, "valueType": "weekly"},
                {"id": "search-term-freeform", "value": term, "valueType": None},
            ]
            for term in ["shoe rack", "shoe organizer"]
        ]
        assert client.post(url, json={"query_mode": "product_term"}).json()["id"] == saved["id"]
        assert len(requests) == 2
        assert client.get(url + "/" + saved["id"]).json()["stale"] is False


def test_product_terms_reject_historical_week_and_legacy_selection(
    db: str, engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 抓住静默替换用户周参数，以及把旧词根误当新商品词来源。
    frozen_date(monkeypatch, "2026-10-29T12:00:00")
    provider = AbaClient(tmp_path / "missing.json")
    with TestClient(create_app(db, storage_root=tmp_path, aba_client=provider)) as client:
        project, run = seed_product_terms(client, engine, tmp_path)
        url = f"/api/v1/projects/{project}/pipeline-runs/{run}/aba-reports"
        for week in ["2026-10-17", "2026-10-31"]:
            assert (
                client.post(url, json={"query_mode": "product_term", "week_end": week}).status_code
                == 422
            )
        with Repository(engine, tmp_path).connect() as conn:
            saved = json.loads(
                conn.execute(
                    "SELECT document FROM root_candidates WHERE run_id=?", (run,)
                ).fetchone()[0]
            )
            saved["rule_version"] = "legacy-root-v1"
            conn.execute(
                "UPDATE root_candidates SET document=? WHERE run_id=?", (json.dumps(saved), run)
            )
        response = client.post(url, json={"query_mode": "product_term"})
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "PRODUCT_TERM_CANDIDATES_REQUIRED"


def test_candidates_include_noncontaining_terms_merge_sources_and_gate_rank(db: str, engine: Engine, tmp_path: Path) -> None:
    # 抓住旧包含商品词规则、只读第一路、未归一化去重、250000 边界丢失与截断。
    def handle(request: httpx.Request) -> httpx.Response:
        term = json.loads(request.content)["filterSelections"][2]["value"]
        rows = (
            [row("  Storage   BENCH ", 250000), row("too rare", 250001)]
            if term == "shoe rack"
            else [row("Ｓｔｏｒａｇｅ bench", 250000), row("entryway", 7)]
        )
        return httpx.Response(200, json={"reportsV2": [{"totalItems": len(rows), "rows": rows}]})

    provider = AbaClient(auth_file(tmp_path), transport=httpx.MockTransport(handle))
    with TestClient(create_app(db, storage_root=tmp_path, aba_client=provider)) as client:
        project, run = seed_product_terms(client, engine, tmp_path)
        service = AbaService(Repository(engine, tmp_path), provider)
        report = service.collect(UUID(project), UUID(run), AbaRequest(query_mode="product_term"))
        items = CoreService(Repository(engine, tmp_path), service, None).hot_search_candidates(
            UUID(project), UUID(run), report.id
        )
        assert [(i.keyword, i.search_frequency_rank, i.source_product_terms) for i in items] == [
            ("entryway", 7, ["shoe organizer"]),
            ("Storage BENCH", 250000, ["shoe rack", "shoe organizer"]),
        ]
        assert len({i.candidate_id for i in items}) == 2
        again = CoreService(Repository(engine, tmp_path), service, None).hot_search_candidates(
            UUID(project), UUID(run), report.id
        )
        assert [i.candidate_id for i in again] == [i.candidate_id for i in items]
        response = client.get(
            f"/api/v1/projects/{project}/pipeline-runs/{run}/aba-reports/{report.id}/hot-search-candidates"
        )
        assert response.status_code == 200
        assert len(response.json()) == 2


@pytest.mark.parametrize("ranks", [(9, 10), (250001, 250002), (250000, 250001)])
def test_conflicting_normalized_ranks_return_409_before_filtering(
    db: str, engine: Engine, tmp_path: Path, ranks: tuple[int, int]
) -> None:
    # 抓住保留首行/最小排名，以及门控先行掩盖超阈值冲突。
    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "reportsV2": [
                    {
                        "totalItems": 2,
                        "rows": [
                            row("storage bench", ranks[0]),
                            row(" STORAGE   bench ", ranks[1]),
                        ],
                    }
                ]
            },
        )

    with TestClient(
        create_app(
            db, aba_client=AbaClient(auth_file(tmp_path), transport=httpx.MockTransport(handle))
        )
    ) as client:
        project, run = seed_product_terms(client, engine, tmp_path)
        url = f"/api/v1/projects/{project}/pipeline-runs/{run}/aba-reports"
        report = client.post(url, json={"query_mode": "product_term"})
        assert report.status_code == 200, report.text
        response = client.get(url + "/" + report.json()["id"] + "/hot-search-candidates")
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "CORE_RANK_CONFLICT"


def test_legacy_requests_and_report_json_remain_readable(db: str, engine: Engine, ) -> None:
    # 抓住新增字段变必填，破坏历史 root/asin JSON。
    for mode in ["root", "asin"]:
        assert (
            AbaRequest.model_validate({"week_end": "2026-10-17", "query_mode": mode}).query_mode
            == mode
        )
        report = AbaReport.model_validate(
            {
                "id": str(uuid4()),
                "project_id": str(uuid4()),
                "run_id": str(uuid4()),
                "week_start": "2026-10-11",
                "week_end": "2026-10-17",
                "fetched_at": "2026-10-18T00:00:00Z",
                "roots_snapshot": ["rack"],
                "query_mode": mode,
                "max_rows": 100,
                "reports": [],
            }
        )
        assert report.query_terms_snapshot == []
        assert report.rule_version == "legacy-root-v1"
