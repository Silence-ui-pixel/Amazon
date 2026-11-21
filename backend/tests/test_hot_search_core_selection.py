"""Real persisted selections: catch caps, wrong eligibility, stale sources and lost evidence."""

import json
from collections.abc import Generator, Iterable
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from test_aba import auth_file, row
from test_aba_expansion import wait
from test_hot_search_core_analysis import setup_analysis

from app.adapters.aba import AbaClient
from app.db.repository import Repository
from app.domain.aba_expansion import ExpansionReport
from app.domain.core_terms import CoreSelectionInput, ExactIntentEvidence
from app.domain.product_terms import TermSummary
from app.domain.verification import SearchCard, SearchPage
from app.main import create_app
from app.services.aba_expansion import ExpansionService
from app.services.asin_product import AmazonProductProvider
from app.services.competitors import CompetitorService
from app.services.core_terms import CoreService
from app.services.hot_search_core_analysis import (
    HotSearchCoreAnalysis,
    HotSearchCoreAnalysisService,
)
from app.services.sellersprite_collection import SellerspriteCollectionService
from app.services.third_party import ThirdPartyService


def ready_selection(
    db: str, engine: Engine, tmp_path: Path,
) -> tuple[HotSearchCoreAnalysisService, UUID, UUID, HotSearchCoreAnalysis]:
    service, project, run, report = setup_analysis(
        db, engine, tmp_path, [f"shoe storage {i:02d}" for i in range(41)]
    )
    analysis = service.start(project, run, report.id)
    for item in analysis.items:
        item.status = "completed"
        item.brand_category = "generic"
        item.intent_evidence = ExactIntentEvidence(
            object_match=True,
            purpose_match=True,
            product_level_match=True,
            modifiers_compatible=True,
            category="exact",
            reasons={
                key: "shoe storage"
                for key in (
                    "object_match",
                    "purpose_match",
                    "product_level_match",
                    "modifiers_compatible",
                )
            },
        )
        item.summary = TermSummary(
            valid_samples=25,
            high_related_count=16,
            relevance_percent=Decimal("64"),
            confidence="high",
        )
    analysis.items[-1].brand_category = "competitor_brand"
    analysis.recommended_candidate_ids = [item.candidate_id for item in analysis.items[:40]]
    analysis.status, analysis.complete = "completed", True
    save_analysis(service.repository, analysis)
    return service, project, run, analysis


def save_analysis(repository: Repository, analysis: HotSearchCoreAnalysis) -> None:
    with repository.connect() as conn:
        conn.execute(
            "UPDATE hot_search_core_analyses SET document=? WHERE id=?",
            (analysis.model_dump_json(), str(analysis.id)),
        )


def body(
    analysis: HotSearchCoreAnalysis, ids: Iterable[UUID] | None = None, version: int = 0
) -> dict[str, object]:
    return {
        "analysis_id": str(analysis.id),
        "rule_version": "hot-search-core-v1",
        "candidate_ids": [
            str(key)
            for key in (ids if ids is not None else reversed(analysis.recommended_candidate_ids))
        ],
        "expected_version": version,
    }


def test_saves_all_forty_in_analysis_order_with_evidence_and_restarts(
    db: str, engine: Engine, tmp_path: Path
) -> None:
    # A 20-item input cap, request-order persistence, or dropping evidence breaks this contract.
    service, project, run, analysis = ready_selection(db, engine, tmp_path)
    base = f"/api/v1/projects/{project}/pipeline-runs/{run}"
    with TestClient(create_app(db, storage_root=tmp_path)) as api:
        response = api.put(base + "/core-selection", json=body(analysis))
        assert response.status_code == 200, response.text
        saved = response.json()
        assert [item["keyword"] for item in saved["items"]] == [
            f"shoe storage {i:02d}" for i in range(40)
        ]
        assert saved["rule_version"] == "hot-search-core-v1"
        assert saved["source_analysis_id"] == str(analysis.id)
        assert saved["items"][0]["intent_evidence"]["category"] == "exact"
        assert saved["items"][0]["summary"]["relevance_percent"] == "64"
        assert api.put(base + "/core-selection", json=body(analysis)).json() == saved
        conflict = api.put(
            base + "/core-selection", json=body(analysis, analysis.recommended_candidate_ids[:2])
        )
        assert conflict.status_code == 409
        subset = api.put(
            base + "/core-selection",
            json=body(analysis, list(reversed(analysis.recommended_candidate_ids[:2])), 1),
        )
        assert subset.status_code == 200, subset.text
        assert subset.json()["version"] == 2
        assert [item["keyword"] for item in subset.json()["items"]] == [
            "shoe storage 00",
            "shoe storage 01",
        ]
    with TestClient(create_app(db, storage_root=tmp_path)) as api:
        assert api.get(base + "/core-selection").json() == subset.json()


@pytest.mark.parametrize(
    "change", ["brand", "foreign", "running", "queued", "failed", "stale", "older"]
)
def test_rejects_ineligible_or_noncurrent_analysis(
    db: str, engine: Engine, tmp_path: Path, change: str
) -> None:
    # Accepting recommended IDs without recomputing gates, or using an old run analysis is unsafe.
    service, project, run, analysis = ready_selection(db, engine, tmp_path)
    selected = analysis.recommended_candidate_ids[:1]
    if change == "brand":
        selected = [analysis.items[-1].candidate_id]
    elif change == "foreign":
        selected = [uuid4()]
    elif change in ("running", "queued", "failed"):
        analysis.status = change
        save_analysis(service.repository, analysis)
    elif change == "stale":
        with service.repository.connect() as conn:
            conn.execute("UPDATE product_profiles SET version=2")
            document = json.loads(
                conn.execute("SELECT document FROM product_profiles").fetchone()[0]
            )
            document["version"] = 2
            conn.execute("UPDATE product_profiles SET document=?", (json.dumps(document),))
    elif change == "older":
        service.start(project, run, analysis.report_id, ["another brand"])
    base = f"/api/v1/projects/{project}/pipeline-runs/{run}"
    with TestClient(create_app(db, storage_root=tmp_path)) as api:
        # Startup recovery turns interrupted running work into a terminal partial result.
        # Set the live state after startup to exercise the actual running rejection.
        if change == "running":
            analysis.status = "running"
            save_analysis(service.repository, analysis)
        response = api.put(base + "/core-selection", json=body(analysis, selected))
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] in {
            "INVALID_CORE_SELECTION",
            "CORE_ANALYSIS_NOT_CURRENT",
        }
        assert api.get(base + "/core-selection").status_code == 404


def test_terminal_partial_analysis_keeps_eligible_subset(
    db: str, engine: Engine, tmp_path: Path
) -> None:
    service, project, run, analysis = ready_selection(db, engine, tmp_path)
    analysis.status, analysis.complete = "incomplete", False
    save_analysis(service.repository, analysis)
    base = f"/api/v1/projects/{project}/pipeline-runs/{run}"
    with TestClient(create_app(db, storage_root=tmp_path)) as api:
        response = api.put(base + "/core-selection", json=body(analysis))
        assert response.status_code == 200, response.text
        assert len(response.json()["items"]) == 40


def test_manual_fallback_accepts_ranked_candidates_when_automatic_gates_select_none(
    db: str, engine: Engine, tmp_path: Path
) -> None:
    """自动门槛全空时，人工仍可确认当前分析中 ABA 排名 25 万以内的候选。"""
    service, project, run, analysis = ready_selection(db, engine, tmp_path)
    chosen = analysis.items[-1]
    chosen.brand_category = "competitor_brand"
    chosen.exclusion_reasons = ["brand_not_generic"]
    analysis.recommended_candidate_ids = []
    analysis.status, analysis.complete = "completed", True
    save_analysis(service.repository, analysis)

    base = f"/api/v1/projects/{project}/pipeline-runs/{run}"
    with TestClient(create_app(db, storage_root=tmp_path)) as api:
        response = api.put(base + "/core-selection", json=body(analysis, [chosen.candidate_id]))
        assert response.status_code == 200, response.text
        saved = response.json()
        assert [item["keyword"] for item in saved["items"]] == [chosen.keyword]
        assert saved["items"][0]["recommended"] is False
        assert "人工" in saved["items"][0]["reason"]


def test_guard_allows_ten_thousand_ids_but_rejects_overflow(db: str, ) -> None:
    # The safety cap must not silently become another business/pagination limit.
    ids = [uuid4() for _ in range(10000)]
    data = CoreSelectionInput(analysis_id=uuid4(), candidate_ids=ids, expected_version=0)
    assert len(data.candidate_ids) == 10000
    with pytest.raises(ValueError):
        CoreSelectionInput(analysis_id=uuid4(), candidate_ids=ids + [uuid4()], expected_version=0)
    with pytest.raises(ValueError):
        CoreSelectionInput(analysis_id=uuid4(), candidate_ids=[ids[0], ids[0]], expected_version=0)
    with pytest.raises(ValueError):
        CoreSelectionInput(analysis_id=uuid4(), candidate_ids=[], expected_version=0)


def test_all_forty_reach_competitor_aba_and_sellersprite_tasks(
    db: str, engine: Engine, tmp_path: Path
) -> None:
    # Removing items or introducing [:10]/[:20] breaks observable downstream coverage.
    service, project, run, analysis = ready_selection(db, engine, tmp_path)
    expected = [f"shoe storage {i:02d}" for i in range(40)]
    base = f"/api/v1/projects/{project}/pipeline-runs/{run}"

    class Search:
        @contextmanager
        def open(self, marketplace: str, evidence_dir: Path) -> Generator["Search"]:
            yield self

        def search(self, keyword: str, page_number: int, evidence_name: str) -> SearchPage:
            return SearchPage(
                region="US", next_page=False, cards=[SearchCard(asin="B000000001", position=1)]
            )

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"reportsV2": [{"totalItems": 1, "rows": [row("expanded", 1)]}]}
        )

    aba = AbaClient(auth_file(tmp_path), transport=httpx.MockTransport(handle))
    with TestClient(
        create_app(
            db,
            storage_root=tmp_path,
            search_provider=Search(),
            aba_client=aba,
            auto_execute=False,
        )
    ) as api:
        saved = api.put(base + "/core-selection", json=body(analysis))
        assert saved.status_code == 200, saved.text
        discovered = api.post(
            base + "/competitors/discover", json={"source": "front_search", "expected_version": 0}
        )
        assert discovered.status_code == 200, discovered.text
        sources = discovered.json()["items"][0]["sources"]
        assert [source["keyword"] for source in sources] == expected
        response = api.post(
            base + "/aba-expansion",
            json={"direction": "forward", "week_end": "2026-10-17", "max_rows": 100},
        )
        assert response.status_code == 200, response.text
        expanded = wait(api, base + "/aba-expansion", response.json()["id"])
        assert [
            query.query for query in ExpansionReport.model_validate(expanded).queries
        ] == expected
        assert api.get(base + "/third-party").json()["forward_queries"] == expected

    core = CoreService(service.repository, service.core.aba, None, hot_analysis_getter=service.get)
    competitors = CompetitorService(service.repository, core, AmazonProductProvider(), Search())
    expansion = ExpansionService(service.repository, aba, core, competitors)
    imports = ThirdPartyService(service.repository, expansion)
    sellersprite = SellerspriteCollectionService(imports, auto_execute=False)
    try:
        from app.domain.sellersprite import BrowserCollectionInput

        tasks = [
            sellersprite.create(
                project, run, BrowserCollectionInput(direction=direction, query=query)
            )
            for direction, query in imports.missing_queries(
                project, run, None, None, "last_30_days"
            )
        ]
        assert [task.request.query for task in tasks] == expected
        assert len(sellersprite.list(project, run)) == 40
    finally:
        sellersprite.close()
        expansion.close()


def test_selection_rechecks_eligibility_inside_write_lock(
    db: str, engine: Engine, tmp_path: Path
) -> None:
    # An analysis changed between initial validation and transaction must never save an old item.
    service, project, run, analysis = ready_selection(db, engine, tmp_path)
    reads = 0

    def changing_analysis(
        project_id: UUID, run_id: UUID, analysis_id: UUID | None
    ) -> HotSearchCoreAnalysis:
        nonlocal reads
        current = service.get(project_id, run_id, analysis_id)
        reads += 1
        if reads == 1:
            analysis.items[0].brand_category = "competitor_brand"
            save_analysis(service.repository, analysis)
        return current

    core = CoreService(
        service.repository, service.core.aba, None, hot_analysis_getter=changing_analysis
    )
    from app.domain.errors import DomainError

    with pytest.raises(DomainError) as error:
        core.select(
            project,
            run,
            CoreSelectionInput.model_validate(
                body(analysis, analysis.recommended_candidate_ids[:1])
            ),
        )
    assert error.value.status_code == 422
    with service.repository.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM core_selections").fetchone()[0] == 0
