"""核心词换版后必须重新发现竞对，不可反复审核过期候选。"""

from dataclasses import replace
from pathlib import Path
from typing import cast
from uuid import UUID

from fastapi import FastAPI
from sqlalchemy import Engine
from test_core_terms import setup_case
from test_verification_jobs import FakeSearch
from test_workflow_auto import fake_auto_model

from app.domain.competitors import DiscoveryInput
from app.domain.core_terms import CoreRequest, CoreSelectionInput
from app.services.amazon_search import SearchCard, SearchPage
from app.workflows.nodes_steps_3_5 import build_nodes_3_5
from app.workflows.service import WorkflowService
from app.workflows.state import initial_state


def test_changed_core_rebuilds_competitors(
    db: str, engine: Engine, tmp_path: Path
) -> None:
    api, base, report_id, _ = setup_case(db, engine, tmp_path)
    try:
        app = cast(FastAPI, api.app)
        service = cast(WorkflowService, app.state.workflow_service)
        deps = replace(service.deps, model=fake_auto_model())
        project, run = UUID(base.split("/")[4]), UUID(base.split("/")[-1])
        analysis = deps.core.analyze(project, run, UUID(report_id), CoreRequest())
        ids = [item.id for item in analysis.items if item.recommended]
        deps.core.select(
            project,
            run,
            CoreSelectionInput(analysis_id=analysis.id, candidate_ids=ids, expected_version=0),
        )
        old = deps.competitors.discover(
            project,
            run,
            DiscoveryInput(
                expected_version=0,
                source="manual",
                asins=["B012345678"],
            ),
        )
        aggregate = deps.core.aggregate(project, run, [analysis.id])
        current = deps.core.select(
            project,
            run,
            CoreSelectionInput(
                analysis_id=aggregate.id,
                candidate_ids=ids,
                expected_version=1,
            ),
        )
        assert deps.competitors.get(project, run).stale
        deps.competitors.search = FakeSearch(
            {
                ("cooling blanket", 1): SearchPage(
                    region="US",
                    cards=[SearchCard(asin="B0ABCDEFG1", position=1)],
                    next_page=False,
                    url="https://www.amazon.com/s?k=cooling+blanket",
                )
            }
        )
        state = initial_state(
            workflow_id="stale-test",
            project_id=str(project),
            run_id=str(run),
            options={"auto_decide": True},
        )
        nodes = build_nodes_3_5(deps)
        blocked = nodes["competitor_selection"](state)
        assert blocked["redo_from"] == "competitor_discovery"
        discovered = nodes["competitor_discovery"](state)
        assert discovered.get("failed_node") is None, discovered
        fresh = deps.competitors.get(project, run)
        assert not fresh.stale and fresh.core_selection_id == current.id
        assert fresh.version > old.version
        assert [item.asin for item in fresh.items] == ["B0ABCDEFG1"]
        confirmed = nodes["competitor_selection"](state)
        assert confirmed.get("failed_node") is None, confirmed
        assert deps.competitors.get(project, run).confirmed_at is not None
    finally:
        api.__exit__(None, None, None)
