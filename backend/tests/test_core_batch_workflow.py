"""自动核心词须完成预算范围内所有分页，再合并确认。"""

from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from fastapi import FastAPI
from sqlalchemy import Engine
from test_core_terms import setup_case

from app.domain.core_terms import CoreAnalysis, CoreCandidates, CoreRequest, CoreSelectionInput
from app.domain.text import phrase_key
from app.workflows.nodes_steps_1_2 import build_nodes_1_2
from app.workflows.service import WorkflowService
from app.workflows.state import initial_state


@pytest.mark.parametrize("max_pages,expected_pages", [(2, 2), (5, 3)])
@pytest.mark.parametrize("duplicate", [False, True])
@pytest.mark.parametrize("reanalyze", [False, True])
def test_core_batch_collection(
    db: str,
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    max_pages: int,
    expected_pages: int,
    duplicate: bool,
    reanalyze: bool,
) -> None:
    api, base, report, _calls = setup_case(db, engine, tmp_path)
    try:
        app = cast(FastAPI, api.app)
        service = cast(WorkflowService, app.state.workflow_service)
        deps = service.deps
        project, run = UUID(base.split("/")[4]), UUID(base.split("/")[-1])
        original = deps.core.candidates(project, run, UUID(report)).items
        recommended = next(item for item in original if item.keyword == "cooling blanket")
        if reanalyze:
            old_analysis = deps.core.analyze(project, run, UUID(report), CoreRequest())
            deps.core.select(
                project,
                run,
                CoreSelectionInput(
                    analysis_id=old_analysis.id,
                    candidate_ids=[i.id for i in old_analysis.items if i.recommended],
                    expected_version=0,
                ),
            )
        seen: list[int] = []

        def candidates(
            _p: UUID, _r: UUID, _report: UUID, offset: int = 0, limit: int = 20
        ) -> CoreCandidates:
            terms = ["cooling blanket", "summer blanket", "light blanket"]
            if duplicate:
                terms[1] = "cooling blanket"
            items = [
                recommended.model_copy(
                    update={
                        "id": UUID(int=index + 100),
                        "keyword": terms[index // 20],
                        "search_frequency_rank": 30 - index // 20,
                    }
                )
                for index in range(60)
            ]
            return CoreCandidates(
                report_id=_report,
                total=60,
                offset=offset,
                limit=limit,
                source_partial=False,
                items=items[offset : offset + limit],
            )

        original_analyze = deps.core.analyze

        def analyze(
            p: UUID, r: UUID, report_id: UUID, request: CoreRequest, *, retry: int = 0
        ) -> CoreAnalysis:
            seen.append(request.offset)
            return original_analyze(p, r, report_id, request, retry=retry)

        monkeypatch.setattr(deps.core, "candidates", candidates)
        monkeypatch.setattr(deps.core, "analyze", analyze)
        state = initial_state(
            workflow_id="batch-test",
            project_id=str(project),
            run_id=str(run),
            options={"auto_decide": True, "core_max_pages": max_pages, "reanalyze_core": reanalyze},
        )
        state["aba_report_id"] = report
        if reanalyze:
            state["aba_report_id"] = None
            state["aba_week_end"] = deps.aba.get(project, run, UUID(report)).week_end.isoformat()

            def no_collection(*_args: object, **_kwargs: object) -> None:
                raise AssertionError("must reuse pinned ABA report")

            monkeypatch.setattr(deps.aba, "collect", no_collection)
            reused = build_nodes_1_2(deps)["collect_core_report"](state)
            assert reused["aba_report_id"] == report
            state["aba_report_id"] = report
        result = build_nodes_1_2(deps)["core_terms"](state)
        assert result.get("failed_node") is None, result
        assert seen == list(range(0, expected_pages * 20, 20))
        selected = deps.core.selection(project, run)
        assert selected.version == (2 if reanalyze else 1)
        state["core_selection_id"] = str(selected.id)
        skipped = build_nodes_1_2(deps)["core_terms"](state)
        assert "已确认核心大词" in str(skipped)
        expected_count = expected_pages - int(duplicate)
        assert len(selected.items) == expected_count
        assert len({phrase_key(item.keyword) for item in selected.items}) == expected_count
        assert selected.items[0].search_frequency_rank == 31 - expected_pages
        aggregate = deps.core.get(project, run, selected.analysis_id)
        assert len(aggregate.source_analysis_ids) == expected_pages
        repeated = deps.core.aggregate(
            project,
            run,
            aggregate.source_analysis_ids,
            screened_candidates=60,
            relevant_candidates=60,
        )
        assert repeated.id == aggregate.id
        assert (
            deps.core.aggregate(
                project,
                run,
                [*aggregate.source_analysis_ids, aggregate.source_analysis_ids[0]],
                screened_candidates=60,
                relevant_candidates=60,
            ).id
            == aggregate.id
        )
    finally:
        api.__exit__(None, None, None)
