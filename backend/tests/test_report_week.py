"""ABA 使用报告周，卖家精灵使用最近30天；周期原样保留。"""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import Engine
from test_aba_expansion import prepare

from app.db.repository import Repository
from app.domain.cleaning import CleaningRequest
from app.domain.sellersprite import BrowserCollectionInput
from app.domain.workflow import WorkflowOptions
from app.main import create_app
from app.workflows.deps import WorkflowDeps
from app.workflows.nodes_common import latest_saturday
from app.workflows.nodes_steps_1_2 import build_nodes_1_2
from app.workflows.state import initial_state


def test_cleaning_accepts_rolling_window_without_inventing_dates(db: str, engine: Engine, ) -> None:
    request = CleaningRequest.model_validate(
        {
            "aba_week_end": "2026-10-17",
            "seller_period": "last_30_days",
            "text_only_acknowledged": True,
        }
    )
    assert request.seller_start is None and request.seller_end is None
    with pytest.raises(ValidationError):
        CleaningRequest.model_validate({**request.model_dump(), "seller_start": "2026-10-01"})


def test_collection_uses_recent_30_days_and_rejects_month_parameter(db: str, engine: Engine, ) -> None:
    request = BrowserCollectionInput.model_validate(
        {"direction": "forward", "query": "gate wheel", "period": "last_30_days"}
    )
    assert request.period == "last_30_days"
    with pytest.raises(ValidationError):
        BrowserCollectionInput.model_validate(
            {"direction": "forward", "query": "gate wheel", "month": "2026-09"}
        )
    CleaningRequest(
        aba_week_end=date(2026, 10, 3),
        seller_start=date(2026, 9, 27),
        seller_end=date(2026, 10, 3),
        text_only_acknowledged=True,
    )


def test_workflow_keeps_selected_aba_week_without_forcing_seller_dates(db: str, engine: Engine, tmp_path: Path) -> None:
    base = prepare(db, engine, tmp_path)
    application = create_app(db, storage_root=tmp_path)
    deps = cast(WorkflowDeps, application.state.workflow_service.deps)
    options = WorkflowOptions.model_validate({"report_week_end": "2026-10-03"})
    state = initial_state(
        workflow_id="w",
        run_id=base.rsplit("/", 1)[-1],
        project_id=base.split("/")[4],
        options=options.model_dump(mode="json"),
    )
    result = build_nodes_1_2(deps)["week"](state)
    assert result["aba_week_end"] == "2026-10-03"
    assert result["seller_start"] is None
    assert result["seller_end"] is None


def test_default_report_week_is_latest_finished_week(db: str, engine: Engine, ) -> None:
    today = datetime.now(UTC).date()
    expected = today - timedelta(days=(today.weekday() - 5) % 7 or 7)
    assert latest_saturday() == expected


def test_legacy_month_cleaning_is_readable_but_stale(db: str, engine: Engine, tmp_path: Path) -> None:
    base = prepare(db, engine, tmp_path)
    run = base.rsplit("/", 1)[-1]
    project = base.split("/")[4]
    key = str(uuid4())
    document: dict[str, object] = {
        "id": key,
        "run_id": run,
        "project_id": project,
        "created_at": datetime.now(UTC).isoformat(),
        "request": {
            "aba_week_end": "2026-10-17",
            "seller_start": "2026-09-01",
            "seller_end": "2026-09-30",
            "text_only_acknowledged": True,
        },
        "sources": [],
        "phrase_count": 0,
        "token_count": 0,
        "frequency_count": 0,
    }
    with Repository(engine, tmp_path).connect() as connection, connection:
        connection.execute(
            "INSERT INTO cleaning_analyses VALUES(?,?,?,?,?,?)",
            (key, run, "legacy", json.dumps(document), "[]", "[]"),
        )
    with TestClient(create_app(db, storage_root=tmp_path)) as api:
        response = api.get(base + "/cleaning")
        assert response.status_code == 200, response.text
        assert response.json()[0]["stale"] is True
        assert response.json()[0]["request"]["seller_start"] == "2026-09-01"


def test_recent_metadata_keeps_collection_time_not_guessed_dates(db: str, engine: Engine, ) -> None:
    from app.domain.third_party import ImportMetadata

    collected = datetime.now(UTC)
    metadata = ImportMetadata(
        direction="forward",
        query="gate wheel",
        marketplace="US",
        period_kind="last_30_days",
        collected_at=collected,
    )
    assert metadata.collected_at == collected
    with pytest.raises(ValidationError):
        ImportMetadata.model_validate({**metadata.model_dump(), "period_start": date(2026, 10, 1)})


def test_workspace_does_not_count_monthly_reports_as_recent(db: str, engine: Engine, tmp_path: Path) -> None:
    from test_third_party import MAPPING, preview

    base = prepare(db, engine, tmp_path)
    with TestClient(create_app(db, storage_root=tmp_path)) as api:
        for direction, query in [
            ("forward", "cooling blanket"),
            ("reverse", "B012345678"),
            ("reverse", "B087654321"),
        ]:
            report = preview(api, base + "/third-party", query, direction)
            assert (
                api.post(
                    base + f"/third-party/{report['id']}/confirm",
                    json={"mapping": MAPPING, "share_unit": "percent"},
                ).status_code
                == 200
            )
        assert api.get(base + "/third-party").json()["complete"] is True
        recent = api.get(base + "/third-party?period_kind=last_30_days").json()
        assert recent["complete"] is False
        assert recent["covered_forward"] == recent["covered_reverse"] == 0
        assert len(recent["reports"]) == 3


def test_auto_collection_keeps_waiting_after_confirmed_truncated_report(
    db: str, engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from langgraph.errors import GraphInterrupt
    from test_third_party import MAPPING, preview

    from app.domain.sellersprite import BrowserCollectionJob
    from app.workflows.nodes_steps_3_5 import build_nodes_3_5

    base = prepare(db, engine, tmp_path)
    application = create_app(db, storage_root=tmp_path)
    with TestClient(application) as api:
        imported = preview(api, base + "/third-party", "cooling blanket", recent=True)
        api.post(
            base + f"/third-party/{imported['id']}/confirm",
            json={"mapping": MAPPING, "share_unit": "percent"},
        )
        deps = cast(WorkflowDeps, application.state.workflow_service.deps)
        run = base.rsplit("/", 1)[-1]
        from uuid import UUID

        report = deps.third_party.get(
            UUID(base.split("/")[4]), UUID(run), UUID(str(imported["id"]))
        )
        report = report.model_copy(update={"complete": False, "truncated": True})

        def fake_get(*_args: object) -> object:
            return report

        monkeypatch.setattr(deps.third_party, "get", fake_get)
        now = datetime.now(UTC)
        job = BrowserCollectionJob(
            id=uuid4(),
            project_id=report.project_id,
            run_id=report.run_id,
            request=BrowserCollectionInput(direction="forward", query="cooling blanket"),
            core_selection_id=report.core_selection_id,
            competitor_id=None,
            status="succeeded",
            report_id=report.id,
            created_at=now,
            updated_at=now,
        )

        def fake_list(*_args: object) -> list[BrowserCollectionJob]:
            return [job]

        def fake_create(*_args: object) -> BrowserCollectionJob:
            return job

        monkeypatch.setattr(deps.sellersprite, "list", fake_list)
        monkeypatch.setattr(deps.sellersprite, "create", fake_create)
        state = initial_state(
            workflow_id="test",
            run_id=run,
            project_id=str(report.project_id),
            options={"third_party_mode": "auto"},
        )
        state["aba_week_end"] = "2026-10-17"

        def pause(_value: object) -> None:
            raise GraphInterrupt()

        monkeypatch.setattr("app.workflows.nodes_steps_3_5.interrupt", pause)
        with pytest.raises(GraphInterrupt):
            build_nodes_3_5(deps)["third_party"](state)
