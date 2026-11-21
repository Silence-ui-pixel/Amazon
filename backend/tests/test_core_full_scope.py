"""默认审核须覆盖第 1000 个候选，确认最多 10 个，不能提前凑数。"""

import json
from pathlib import Path
from typing import cast
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import Engine
from test_core_terms import setup_case

from app.domain.workflow import WorkflowOptions
from app.services.qwen import QwenClient, QwenConfig
from app.workflows.nodes_steps_1_2 import build_nodes_1_2
from app.workflows.service import WorkflowService
from app.workflows.state import initial_state


@pytest.mark.parametrize("qualified", [0, 4, 13, 1000])
@pytest.mark.parametrize("explicit_options", [False, True])
def test_default_full_scope_selects_up_to_ten(
    db: str,
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    qualified: int,
    explicit_options: bool,
) -> None:
    import test_core_terms

    monkeypatch.setattr(
        test_core_terms, "TERMS", [(f"blanket term {i}", i) for i in range(1, 1001)]
    )
    api, base, report, _ = setup_case(db, engine, tmp_path)
    try:
        service = cast(WorkflowService, cast(FastAPI, api.app).state.workflow_service)
        project, run = UUID(base.split("/")[4]), UUID(base.split("/")[-1])

        def model(request: httpx.Request) -> httpx.Response:
            prompt = json.loads(request.content)["messages"][0]["content"][0]["text"]
            data = json.loads(prompt.split("数据：", 1)[1])
            if "相关性粗筛" in prompt:
                output = {"candidate_ids": [i["id"] for i in data["candidates"]]}
            else:
                output = {
                    "items": [
                        dict(
                            candidate_id=i["id"],
                            translation="候选毯子",
                            keyword_role="core",
                            relevance="high"
                            if i["search_frequency_rank"] > 1000 - qualified
                            else "low",
                            intent="exact",
                            category="generic",
                            reason="商品匹配判断",
                        )
                        for i in data["candidates"]
                    ]
                }
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"finish_reason": "stop", "message": {"content": json.dumps(output)}}
                    ]
                },
            )

        service.deps.core.client = QwenClient(
            QwenConfig(api_key="test"), transport=httpx.MockTransport(model)
        )
        options: dict[str, object] = (
            WorkflowOptions().model_dump(mode="json") if explicit_options else {"auto_decide": True}
        )
        state = initial_state(
            workflow_id="full-scope", project_id=str(project), run_id=str(run), options=options
        )
        state["aba_report_id"] = report
        result = build_nodes_1_2(service.deps)["core_terms"](state)
        aggregate = service.deps.core.history(project, run, UUID(report))[0]
        assert len(aggregate.items) == 1000
        assert aggregate.remaining_candidates == 0
        assert len(aggregate.source_analysis_ids) == 50
        assert aggregate.screened_candidates == 1000
        if qualified == 0:
            assert result.get("failed_node") == "core_terms"
            return
        assert not result.get("failed_node"), result
        selection = service.deps.core.selection(project, run)
        assert len(selection.items) == min(qualified, 10)
        assert [i.search_frequency_rank for i in selection.items] == list(
            range(1001 - qualified, 1001 - qualified + min(qualified, 10))
        )
    finally:
        api.__exit__(None, None, None)
