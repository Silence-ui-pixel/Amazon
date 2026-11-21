"""失败原因不可被防重复提示覆盖，处理中请求不能另开付费重试。"""

import json
from pathlib import Path
from typing import cast
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import Engine
from test_core_terms import setup_case

from app.domain.core_terms import CoreRequest
from app.domain.errors import DomainError
from app.domain.product_understanding import ProductUnderstanding
from app.services.qwen import QwenClient, QwenConfig
from app.workflows.nodes_steps_1_2 import build_nodes_1_2
from app.workflows.service import WorkflowService
from app.workflows.state import initial_state


def test_workflow_exhausted_invalid_outputs_do_not_hide_cause(
    db: str, engine: Engine, tmp_path: Path
) -> None:
    api, base, report, calls = setup_case(db, engine, tmp_path, "missing_role")
    try:
        service = cast(WorkflowService, cast(FastAPI, api.app).state.workflow_service)
        core = service.deps.core
        project, run = UUID(base.split("/")[4]), UUID(base.split("/")[-1])
        candidates = core.candidates(project, run, UUID(report)).items
        for attempt in range(3):
            with pytest.raises(DomainError):
                core.analyze(
                    project,
                    run,
                    UUID(report),
                    CoreRequest(candidate_ids=[i.id for i in candidates]),
                    retry=attempt,
                )
        state = initial_state(
            workflow_id="exhausted",
            project_id=str(project),
            run_id=str(run),
            options={"auto_decide": True},
        )
        state["aba_report_id"] = report
        for _ in range(2):
            result = build_nodes_1_2(service.deps)["core_terms"](state)
            assert str(result["error"]).startswith("MODEL_OUTPUT_INVALID")
            assert "keyword_role" in str(result["error"])
        assert len(calls) == 4  # 3 条原失败 + 1 次粗筛，重进不产生新的详细审核。
    finally:
        api.__exit__(None, None, None)


def test_cached_invalid_keeps_cause_and_diagnostics(
    db: str, engine: Engine, tmp_path: Path
) -> None:
    api, base, report, calls = setup_case(db, engine, tmp_path, "missing_role")
    try:
        service = cast(WorkflowService, cast(FastAPI, api.app).state.workflow_service)
        assert service.deps.core.client is not None
        service.deps.core.client.diagnostic_dir = tmp_path / "diagnostics"
        url = base + f"/aba-reports/{report}/core-analyses"
        first = api.post(url, json={})
        again = api.post(url, json={})
        assert first.json()["error"]["code"] == "MODEL_OUTPUT_INVALID"
        assert again.json()["error"]["code"] == "MODEL_OUTPUT_INVALID"
        assert "keyword_role" in again.json()["error"]["message"]
        assert len(calls) == 1
        diagnostics = list((tmp_path / "diagnostics").glob("core-*.json"))
        assert len(diagnostics) == 1
        diagnostic = json.loads(diagnostics[0].read_text(encoding="utf-8"))
        assert diagnostic["raw"]
        assert diagnostic["errors"][0]["loc"][-1] == "keyword_role"
        assert "test-secret" not in diagnostics[0].read_text(encoding="utf-8")
    finally:
        api.__exit__(None, None, None)


def test_running_claim_cannot_launch_another_retry(
    db: str, engine: Engine, tmp_path: Path
) -> None:
    api, base, report, calls = setup_case(db, engine, tmp_path)
    try:
        service = cast(WorkflowService, cast(FastAPI, api.app).state.workflow_service)
        core = service.deps.core
        project, run = UUID(base.split("/")[4]), UUID(base.split("/")[-1])
        candidates = core.candidates(project, run, UUID(report)).items
        analysis = core.analyze(
            project, run, UUID(report), CoreRequest(candidate_ids=[i.id for i in candidates])
        )
        analysis.status = "running"
        with core.repository.connect() as conn:
            conn.execute(
                "UPDATE core_analyses SET document=? WHERE id=?",
                (analysis.model_dump_json(), str(analysis.id)),
            )
        state = initial_state(
            workflow_id="claimed",
            project_id=str(project),
            run_id=str(run),
            options={"auto_decide": True},
        )
        state["aba_report_id"] = report
        result = build_nodes_1_2(service.deps)["core_terms"](state)
        assert result.get("failed_node") == "core_terms"
        assert str(result["error"]).startswith("CORE_ANALYSIS_ALREADY_ATTEMPTED")
        assert len(calls) == 2  # 原分析一次 + 粗筛一次，不能另开 detailed retry=1。
    finally:
        api.__exit__(None, None, None)


def test_model_receives_complete_required_output_schema() -> None:
    def model(request: httpx.Request) -> httpx.Response:
        prompt = json.loads(request.content)["messages"][0]["content"][0]["text"]
        schema_present = '"required"' in prompt and '"keyword_role"' in prompt
        item = dict(
            candidate_id=json.loads(prompt.split("数据：", 1)[1])["candidates"][0]["id"],
            translation="门轮",
            relevance="high",
            intent="exact",
            category="generic",
            reason="商品品类",
        )
        if schema_present:
            item["keyword_role"] = "core"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"finish_reason": "stop", "message": {"content": json.dumps({"items": [item]})}}
                ]
            },
        )

    from app.domain.core_terms import CoreCandidate

    client = QwenClient(QwenConfig(api_key="test"), transport=httpx.MockTransport(model))
    result = client.analyze_core_terms(
        ProductUnderstanding(
            product_name="门轮", product_type="轮子", features=[], uncertainties=[]
        ),
        [
            CoreCandidate(
                id=UUID(int=1),
                keyword="gate wheel",
                search_frequency_rank=10,
                matched_roots=["wheel"],
                root_reference=False,
            )
        ],
        [],
    )
    assert result.items[0].keyword_role == "core"
