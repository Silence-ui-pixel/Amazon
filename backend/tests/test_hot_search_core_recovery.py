"""No refresh, duplicate POST or restart may reissue an unknown paid request."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import cast

import httpx
import pytest
from fastapi.testclient import TestClient
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from sqlalchemy import Engine
from test_hot_search_core_analysis import Search, execute, setup_analysis

from app.domain.errors import DomainError
from app.main import create_app
from app.services.hot_search_core_analysis import HotSearchCoreAnalysis
from app.workflows.deps import WorkflowDeps
from app.workflows.graph import build_graph
from app.workflows.state import initial_state


def test_default_start_is_queued_and_equivalent_brand_settings_are_idempotent(
    db: str, engine: Engine, tmp_path: Path,
) -> None:
    # Catches synchronous POST execution and brand casing/order creating paid duplicates.
    service, project, run, report = setup_analysis(db, engine, tmp_path, count=1)

    def start(brands: list[str]) -> HotSearchCoreAnalysis:
        return service.start(project, run, report.id, brands)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                start,
                [[" MyBrand ", "Other"], ["other", "mybrand", "MYBRAND"]],
            )
        )
    assert results[0].id == results[1].id
    assert results[0].status == "queued"
    assert results[0].batches == []
    assert results[0].own_brands_snapshot == ["mybrand", "other"]
    result = service.execute(project, run, results[0].id)
    assert service.start(project, run, report.id, ["other", "mybrand"]).id == result.id
    assert service.execute(project, run, result.id).model_dump() == result.model_dump()


def test_recovery_preserves_completed_words_and_fails_unknown_batches(
    db: str, engine: Engine, tmp_path: Path
) -> None:
    service, project, run, report = setup_analysis(
        db, engine, tmp_path,
        ["shoe storage", "shoe organizer"],
        count=1,
        model_mode="crash_second",
    )
    queued = service.start(project, run, report.id, [])
    with pytest.raises(SystemExit):
        service.execute(project, run, queued.id)
    before = service.get(project, run, queued.id)
    completed = next(item for item in before.items if item.status == "completed").model_dump()
    assert any(batch.status == "started" for batch in before.batches)
    service.recover()
    recovered = service.get(project, run, queued.id)
    assert recovered.status == "incomplete"
    assert (
        next(item for item in recovered.items if item.status == "completed").model_dump()
        == completed
    )
    assert {item.status for item in recovered.items} == {"completed", "failed"}
    assert any(batch.status == "outcome_unknown" for batch in recovered.batches)
    assert not any(batch.status in ("planned", "started") for batch in recovered.batches)
    assert service.execute(project, run, queued.id).model_dump() == recovered.model_dump()


def test_never_executed_queue_survives_restart_and_executes_once(
    db: str, engine: Engine, tmp_path: Path
) -> None:
    # Catches recovery permanently failing a queued task before any external operation exists.
    service, project, run, report = setup_analysis(db, engine, tmp_path, count=1)
    queued = service.start(project, run, report.id, [])
    assert queued.batches == []

    service.recover()
    repeated = service.start(project, run, report.id, [])
    assert repeated.id == queued.id
    assert repeated.status == "queued"
    assert repeated.batches == []

    completed = service.execute(project, run, repeated.id)
    assert completed.status == "completed"
    assert completed.items[0].status == "completed"
    assert {
        kind: sum(batch.kind == kind for batch in completed.batches)
        for kind in (
            "open",
            "intent",
            "collection",
            "search",
            "product",
            "comparison",
        )
    } == {
        "open": 1,
        "intent": 1,
        "collection": 1,
        "search": 1,
        "product": 1,
        "comparison": 1,
    }
    assert all(batch.status == "completed" for batch in completed.batches)
    assert service.execute(project, run, queued.id).model_dump() == completed.model_dump()


@pytest.mark.parametrize("change", ["profile", "selection", "delivery", "report"])
def test_changed_input_marks_old_analysis_stale_without_rewriting_evidence(
    db: str, engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    service, project, run, report = setup_analysis(db, engine, tmp_path, count=1)
    original = execute(service, project, run, report)
    with service.repository.connect() as conn:
        stored_before = conn.execute(
            "SELECT document FROM hot_search_core_analyses WHERE id=?", (str(original.id),)
        ).fetchone()[0]
        if change == "profile":
            profile = json.loads(
                conn.execute(
                    "SELECT document FROM product_profiles WHERE run_id=?", (str(run),)
                ).fetchone()[0]
            )
            profile["version"] = 2
            conn.execute(
                "INSERT INTO product_profiles VALUES(?,?,?,?)",
                (str(run), 2, "confirmed", json.dumps(profile)),
            )
        elif change == "selection":
            selection = json.loads(
                conn.execute(
                    "SELECT document FROM root_selections WHERE run_id=?", (str(run),)
                ).fetchone()[0]
            )
            selection["candidate_ids"] = selection["candidate_ids"][:1]
            conn.execute(
                "UPDATE root_selections SET document=? WHERE run_id=?",
                (json.dumps(selection), str(run)),
            )
        elif change == "report":
            saved = report.model_dump(mode="json")
            saved["reports"][0]["rows"][0]["search_frequency_rank"] = 7
            conn.execute(
                "UPDATE aba_reports SET document=? WHERE id=?", (json.dumps(saved), str(report.id))
            )
        else:
            monkeypatch.setenv("KEYWORD_US_DELIVERY_ZIP", "90210")
    stale = service.get(project, run, original.id)
    assert stale.status == "stale"
    assert stale.stale is True
    assert stale.items == original.items
    assert stale.recommended_candidate_ids == original.recommended_candidate_ids
    with service.repository.connect() as conn:
        assert (
            conn.execute(
                "SELECT document FROM hot_search_core_analyses WHERE id=?", (str(original.id),)
            ).fetchone()[0]
            == stored_before
        )


def test_new_brand_settings_produce_new_analysis_and_preserve_old_history(
    db: str, engine: Engine, tmp_path: Path
) -> None:
    service, project, run, report = setup_analysis(db, engine, tmp_path, count=1)
    original = execute(service, project, run, report)
    newer = service.start(project, run, report.id, ["shoe"])
    assert newer.id != original.id
    assert service.get(project, run).id == newer.id
    assert service.get(project, run, original.id).items == original.items
    assert service.get(project, run, original.id).own_brands_snapshot == []


def test_return_to_original_brands_reactivates_evidence_and_survives_restart(
    db: str, engine: Engine, tmp_path: Path,
) -> None:
    service, project, run, report = setup_analysis(db, engine, tmp_path, count=1)
    original = execute(service, project, run, report)
    newer = service.start(project, run, report.id, ["Acme"])
    assert service.get(project, run).id == newer.id
    reused = service.start(project, run, report.id, [])
    assert reused.model_dump() == original.model_dump()
    assert service.get(project, run).id == original.id
    assert service.execute(project, run, reused.id).batches == original.batches
    base = f"/api/v1/projects/{project}/pipeline-runs/{run}"
    with TestClient(create_app(db, storage_root=tmp_path)) as api:
        assert api.get(base + "/hot-search-core-analysis").json()["id"] == str(original.id)
        saved = api.put(
            base + "/core-selection",
            json={
                "analysis_id": str(original.id),
                "rule_version": "hot-search-core-v1",
                "candidate_ids": [str(key) for key in original.recommended_candidate_ids],
                "expected_version": 0,
            },
        )
        assert saved.status_code == 200, saved.text
        assert api.get(base + "/core-selection").json()["stale"] is False
    with TestClient(create_app(db, storage_root=tmp_path)) as api:
        assert api.get(base + "/hot-search-core-analysis").json() == original.model_dump(
            mode="json"
        )
        assert api.get(base + "/core-selection").json()["stale"] is False


def test_missing_current_pointer_can_be_reactivated_without_overwriting_history(
    db: str, engine: Engine, tmp_path: Path,
) -> None:
    service, project, run, report = setup_analysis(db, engine, tmp_path, count=1)
    original = service.start(project, run, report.id)
    service.start(project, run, report.id, ["Acme"])
    with service.repository.connect() as conn:
        conn.execute("DELETE FROM hot_search_core_current WHERE run_id=?", (str(run),))
    with pytest.raises(DomainError):
        service.get(project, run)
    service.start(project, run, report.id)
    assert service.get(project, run).id == original.id


def test_profile_change_allows_explicit_new_api_analysis_preserving_old_evidence(
    db: str, engine: Engine, tmp_path: Path,
) -> None:
    service, project, run, report = setup_analysis(db, engine, tmp_path, count=1)
    old = execute(service, project, run, report)
    with service.repository.connect() as conn:
        evidence = conn.execute(
            "SELECT document FROM hot_search_core_analyses WHERE id=?", (str(old.id),)
        ).fetchone()[0]
        profile = json.loads(
            conn.execute(
                "SELECT document FROM product_profiles WHERE run_id=?", (str(run),)
            ).fetchone()[0]
        )
        profile["version"] = 2
        conn.execute(
            "INSERT INTO product_profiles VALUES(?,?,?,?)",
            (str(run), 2, "confirmed", json.dumps(profile)),
        )
    base = f"/api/v1/projects/{project}/pipeline-runs/{run}"
    with TestClient(
        create_app(
            db,
            storage_root=tmp_path,
            model_client=service.client,
            search_provider=Search(service.repository, count=1),
        )
    ) as api:
        stale = api.get(base + "/hot-search-core-analysis").json()
        assert stale["status"] == "stale"
        assert api.get(base + f"/aba-reports/{report.id}").json()["stale"] is False
        queued = api.post(
            base + "/hot-search-core-analyses", json={"report_id": str(report.id)}
        ).json()
        assert queued["id"] != str(old.id)
        assert queued["status"] == "queued"
        assert queued["profile_version"] == 2
        url = base + "/hot-search-core-analyses/" + queued["id"]
        assert api.post(url + "/execute").status_code == 202
        completed = api.get(url).json()
        assert completed["status"] == "completed"
        api.post(url + "/execute")
        assert api.get(url).json() == completed
        assert api.get(base + "/hot-search-core-analysis").json()["id"] == queued["id"]
    with service.repository.connect() as conn:
        assert (
            conn.execute(
                "SELECT document FROM hot_search_core_analyses WHERE id=?", (str(old.id),)
            ).fetchone()[0]
            == evidence
        )


def test_concurrent_explicit_execution_returns_running_without_second_dispatch(
    db: str, engine: Engine, tmp_path: Path,
) -> None:
    service, project, run, report = setup_analysis(db, engine, tmp_path, count=1)
    assert service.client is not None
    transport = service.client.transport
    assert isinstance(transport, httpx.MockTransport)
    entered, release = Event(), Event()
    requests: list[str] = []

    def blocked(request: httpx.Request) -> httpx.Response:
        requests.append(request.content.decode())
        if len(requests) == 1:
            entered.set()
            assert release.wait(10), "test must release the external response"
        return transport.handle_request(request)

    service.client.transport = httpx.MockTransport(blocked)
    base = f"/api/v1/projects/{project}/pipeline-runs/{run}"
    with TestClient(
        create_app(
            db,
            storage_root=tmp_path,
            model_client=service.client,
            search_provider=service.search,
        )
    ) as api:
        queued = api.post(
            base + "/hot-search-core-analyses", json={"report_id": str(report.id)}
        ).json()
        url = base + "/hot-search-core-analyses/" + queued["id"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(api.post, url + "/execute")
            try:
                assert entered.wait(10)
                repeated = api.post(url + "/execute")
                assert repeated.status_code == 202
                assert repeated.json()["status"] == "running"
                assert len(requests) == 1
            finally:
                release.set()
            assert first.result().status_code == 202
        assert api.get(url).json()["status"] == "completed"
        assert len(requests) == 2  # one intent request and one comparison request


def test_api_post_queues_and_both_get_routes_only_read(
    db: str, engine: Engine, tmp_path: Path
) -> None:
    service, project, run, report = setup_analysis(db, engine, tmp_path, count=1)
    app = create_app(
        db,
        storage_root=tmp_path,
        model_client=service.client,
        search_provider=service.search,
        auto_execute=False,
    )
    url = f"/api/v1/projects/{project}/pipeline-runs/{run}"
    with TestClient(app) as client:
        created = client.post(
            url + "/hot-search-core-analyses", json={"report_id": str(report.id), "own_brands": []}
        )
        assert created.status_code == 200, created.text
        queued = created.json()
        assert queued["status"] == "queued"
        for path in ["/hot-search-core-analysis", "/hot-search-core-analyses/" + queued["id"]]:
            refreshed = client.get(url + path)
            assert refreshed.status_code == 200, refreshed.text
            assert refreshed.json() == queued
            assert refreshed.json()["batches"] == []
        repeated = client.post(
            url + "/hot-search-core-analyses", json={"report_id": str(report.id)}
        )
        assert repeated.json()["id"] == queued["id"]


def test_analysis_cannot_be_read_through_another_project(
    db: str, engine: Engine, tmp_path: Path
) -> None:
    from app.domain.errors import DomainError
    from app.domain.schemas import ProjectCreate

    service, project, run, report = setup_analysis(db, engine, tmp_path, count=1)
    original = service.start(project, run, report.id, [])
    another = service.repository.create_project(ProjectCreate(name="other", marketplace="US"))
    with pytest.raises(DomainError) as error:
        service.get(another.id, run, original.id)
    assert error.value.status_code == 404


@pytest.mark.parametrize("partial", [False, True])
def test_explicit_api_execution_finishes_and_saves_without_repeated_calls(
    db: str, engine: Engine, tmp_path: Path, partial: bool
) -> None:
    service, project, run, report = setup_analysis(
        db, engine, tmp_path,
        ["bad search", "shoe storage"] if partial else None,
        count=1,
        failing=["bad search"] if partial else [],
    )
    base = f"/api/v1/projects/{project}/pipeline-runs/{run}"
    app = create_app(
        db,
        storage_root=tmp_path,
        model_client=service.client,
        search_provider=service.search,
        auto_execute=False,
    )
    with TestClient(app) as api:
        graph = build_graph(cast(WorkflowDeps, app.state.workflow_service.deps), InMemorySaver())
        state = initial_state(
            workflow_id="manual-core",
            project_id=str(project),
            run_id=str(run),
            options={"auto_decide": False},
        )
        state["aba_report_id"] = str(report.id)
        state["core_rule_version"] = "hot-search-core-v1"
        config: RunnableConfig = {"configurable": {"thread_id": "manual-core"}}
        graph.update_state(config, dict(state), as_node="collect_core_report")
        graph.invoke(None, config, durability="sync")
        assert graph.get_state(config).next == ("core_terms",)
        queued = api.post(
            base + "/hot-search-core-analyses", json={"report_id": str(report.id)}
        ).json()
        assert queued["status"] == "queued"
        assert api.get(base + "/hot-search-core-analysis").json()["batches"] == []
        url = base + "/hot-search-core-analyses/" + queued["id"]
        response = api.post(url + "/execute")
        assert response.status_code == 202, response.text
        result = api.get(url).json()
        assert result["status"] == ("incomplete" if partial else "completed")
        assert result["recommended_candidate_ids"]
        api.post(url + "/execute")
        assert api.get(url).json() == result
        assert (
            api.post(base + "/hot-search-core-analyses", json={"report_id": str(report.id)}).json()
            == result
        )
        saved = api.put(
            base + "/core-selection",
            json={
                "analysis_id": result["id"],
                "rule_version": "hot-search-core-v1",
                "candidate_ids": result["recommended_candidate_ids"],
                "expected_version": 0,
            },
        )
        assert saved.status_code == 200, saved.text
        assert len(saved.json()["items"]) == 1
        resumed = graph.invoke(Command(resume={}), config, durability="sync")
        assert resumed["core_selection_id"] == saved.json()["id"]
        assert "core_terms" not in graph.get_state(config).next
        assert api.get(url).json() == result
