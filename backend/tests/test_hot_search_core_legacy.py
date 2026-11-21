"""历史 JSON 和图保持原周报、原分析及原选择语义。"""

import json
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from fastapi import FastAPI
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import Engine
from test_core_terms import setup_case
from test_workflow_auto import apply_step

from app.workflows.deps import WorkflowDeps
from app.workflows.graph import build_graph
from app.workflows.state import WorkflowState, initial_state


@pytest.mark.parametrize("product_terms", [False, True])
def test_legacy_checkpoint_restores_report_and_selection_without_new_analysis(
    db: str,
    engine: Engine,
    tmp_path: Path,
    product_terms: bool,
) -> None:
    # Catches treating absent rule metadata as product-term-v1 or replaying paid work.
    client, base, report_id, calls = setup_case(db, engine, tmp_path)
    try:
        deps = cast(WorkflowDeps, cast(FastAPI, client.app).state.workflow_service.deps)
        parts = base.split("/")
        project, run = UUID(parts[4]), UUID(parts[6])
        state = initial_state(
            workflow_id="legacy",
            project_id=str(project),
            run_id=str(run),
            options={"auto_decide": True, "own_brands": ["Acme"]},
        )
        state["aba_report_id"] = report_id
        state["aba_week_end"] = "2026-10-17"
        apply_step(deps, state, "collect_core_report")
        result = apply_step(deps, state, "core_terms")
        assert result["failed_node"] is None, result
        saved = deps.core.selection(project, run)
        assert saved.rule_version == "core-terms-v1"
        assert [item.keyword for item in saved.items] == ["cooling blanket"]
        count = len(calls)
        if product_terms:
            with deps.repository.connect() as conn:
                document = json.loads(
                    conn.execute(
                        "SELECT document FROM root_candidates WHERE run_id=?", (str(run),)
                    ).fetchone()[0]
                )
                document["rule_version"] = "product-term-v1"
                conn.execute(
                    "UPDATE root_candidates SET document=? WHERE run_id=?",
                    (json.dumps(document), str(run)),
                )
        old_json = {
            key: value
            for key, value in state.items()
            if not key.startswith("hot_search_core") and key != "core_rule_version"
        }
        restored = cast(WorkflowState, json.loads(json.dumps(old_json)))
        apply_step(deps, restored, "collect_core_report")
        result = apply_step(deps, restored, "core_terms")
        assert result["core_selection_id"] == str(saved.id)
        assert restored["aba_report_id"] == report_id
        assert deps.aba.get(project, run, UUID(report_id)).week_end.isoformat() == "2026-10-17"
        assert len(calls) == count
        with deps.repository.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM hot_search_core_analyses").fetchone()[0] == 0
        # Restore the old checkpoint into the real complete graph, from the week boundary.
        graph = build_graph(deps, InMemorySaver())
        restored["options"] = {"auto_decide": False}
        config = graph.update_state(
            {"configurable": {"thread_id": "mixed-legacy"}},
            dict(restored),
            as_node="root_selection",
        )
        result = graph.invoke(None, config, durability="sync")
        assert result["core_selection_id"] == str(saved.id)
        assert result["aba_report_id"] == report_id
        assert result["core_rule_version"] == "core-terms-v1"
        assert len(calls) == count
    finally:
        client.__exit__(None, None, None)
