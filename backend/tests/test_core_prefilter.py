"""粗筛必须覆盖排名靠后候选，长尾不得冒充核心；计费结果持久化。"""

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
from app.services.qwen import QwenClient, QwenConfig
from app.workflows.nodes_steps_1_2 import build_nodes_1_2
from app.workflows.service import WorkflowService
from app.workflows.state import initial_state


def test_model_must_supply_keyword_role(db: str, engine: Engine, tmp_path: Path) -> None:
    api, base, report, _ = setup_case(db, engine, tmp_path, "missing_role")
    try:
        # 历史 CoreTerm 可读默认值，但新模型响应漏掉角色必须拒绝。
        response = api.post(base + f"/aba-reports/{report}/core-analyses", json={})
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "MODEL_OUTPUT_INVALID"
    finally:
        api.__exit__(None, None, None)


@pytest.mark.parametrize("mode", ["ok", "timeout", "unknown", "duplicate"])
def test_full_report_prefilter_and_core_roles(
    db: str, engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    import test_core_terms

    monkeypatch.setattr(
        test_core_terms,
        "TERMS",
        [
            *[(f"electric blanket {i}", i + 1) for i in range(493)],
            ("cooling blanket", 494),
            ("blue cooling blanket", 495),
        ],
    )
    api, base, report, _ = setup_case(db, engine, tmp_path)
    try:
        service = cast(WorkflowService, cast(FastAPI, api.app).state.workflow_service)
        core = service.deps.core
        project, run = UUID(base.split("/")[4]), UUID(base.split("/")[-1])
        calls: list[str] = []

        def model(request: httpx.Request) -> httpx.Response:
            prompt = json.loads(request.content)["messages"][0]["content"][0]["text"]
            data = json.loads(prompt.split("数据：", 1)[1])
            coarse = "相关性粗筛" in prompt
            calls.append("coarse" if coarse else "detailed")
            if mode == "timeout":
                raise httpx.ReadTimeout("timeout", request=request)
            ids = [c["id"] for c in data["candidates"] if "electric" not in c["keyword"]]
            if coarse:
                if mode == "unknown":
                    ids = [str(UUID(int=1))]
                if mode == "duplicate" and ids:
                    ids.append(ids[0])
                output = {"candidate_ids": ids}
            else:
                output = {
                    "items": [
                        dict(
                            candidate_id=c["id"],
                            translation="凉感毯",
                            relevance="high",
                            intent="exact",
                            category="generic",
                            reason="符合商品",
                            keyword_role="long_tail" if "blue" in c["keyword"] else "core",
                        )
                        for c in data["candidates"]
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

        core.client = QwenClient(QwenConfig(api_key="test"), transport=httpx.MockTransport(model))
        if mode != "ok":
            with pytest.raises(DomainError):
                core.prefilter(project, run, UUID(report))
            count = len(calls)
            with pytest.raises(DomainError):
                core.prefilter(project, run, UUID(report))
            assert len(calls) == count  # 失败/未知结果不得重发付费调用
            return
        screened = core.prefilter(project, run, UUID(report))
        assert screened.screened_candidates == 495
        assert len(screened.candidate_ids) == 2
        assert core.prefilter(project, run, UUID(report)) == screened
        assert calls == ["coarse"] * 3
        state = initial_state(
            workflow_id="coarse-test",
            project_id=str(project),
            run_id=str(run),
            options={"auto_decide": True, "core_max_pages": 1},
        )
        state["aba_report_id"] = report
        result = build_nodes_1_2(service.deps)["core_terms"](state)
        assert not result.get("failed_node"), result
        selected = core.selection(project, run)
        assert [i.keyword for i in selected.items] == ["cooling blanket"]
        analysis = core.get(project, run, selected.analysis_id)
        assert analysis.screened_candidates == 495
        assert analysis.relevant_candidates == 2
        assert [(i.keyword_role, i.recommended) for i in analysis.items] == [
            ("core", True),
            ("long_tail", False),
        ]
        assert calls == ["coarse"] * 3 + ["detailed"]
        with pytest.raises(DomainError):
            core.analyze(project, run, UUID(report), CoreRequest(candidate_ids=[UUID(int=1)]))
    finally:
        api.__exit__(None, None, None)
