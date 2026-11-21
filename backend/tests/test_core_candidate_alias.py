"""长 UUID 抄错不能阻断已成功批次；模型只负责返回短编号。"""

import json
from pathlib import Path
from typing import cast
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import Engine
from test_core_terms import setup_case

from app.domain.core_terms import CoreCandidate, CoreRequest
from app.domain.errors import DomainError
from app.domain.product_understanding import ProductUnderstanding
from app.services.qwen import QwenClient, QwenConfig
from app.workflows.service import WorkflowService


def test_model_uuid_version_bias_does_not_corrupt_candidate_identity() -> None:
    key = UUID("5968cdee-1c22-5c78-93b4-ab33d37c15c9")

    def model(request: httpx.Request) -> httpx.Response:
        prompt = json.loads(request.content)["messages"][0]["content"][0]["text"]
        candidate = json.loads(prompt.split("数据：", 1)[1])["candidates"][0]
        # 真实失败模式：模型将 UUID v5 误抄成 v4；短编号没有这个偏差。
        candidate_id = candidate["id"].replace("-5c78-", "-4c78-")
        item = dict(
            candidate_id=candidate_id,
            translation="4英寸脚轮",
            relevance="low",
            intent="unrelated",
            category="generic",
            keyword_role="long_tail",
            reason="尺寸不符",
        )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"finish_reason": "stop", "message": {"content": json.dumps({"items": [item]})}}
                ]
            },
        )

    client = QwenClient(QwenConfig(api_key="test"), transport=httpx.MockTransport(model))
    draft = client.analyze_core_terms(
        ProductUnderstanding(
            product_name="门轮", product_type="支撑轮", features=[], uncertainties=[]
        ),
        [
            CoreCandidate(
                id=key,
                keyword="4 inch caster",
                search_frequency_rank=1,
                matched_roots=["caster"],
                root_reference=False,
            )
        ],
        [],
    )
    assert draft.items[0].candidate_id == key


@pytest.mark.parametrize("status", ["succeeded", "running", "outcome_pending", "failed"])
def test_identity_protocol_upgrade_reuses_success_and_protects_unknown(
    db: str, engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    import app.services.core_terms as core_module

    api, base, report, calls = setup_case(db, engine, tmp_path)
    try:
        core = cast(WorkflowService, cast(FastAPI, api.app).state.workflow_service).deps.core
        project, run = UUID(base.split("/")[4]), UUID(base.split("/")[-1])
        monkeypatch.setattr(core_module, "PROMPT_VERSION", "core-terms-v5")
        old = core.analyze(project, run, UUID(report), CoreRequest())
        document = old.model_dump()
        document["status"] = status
        document["error_code"] = "MODEL_OUTPUT_INVALID" if status == "failed" else None
        with core.repository.connect() as conn:
            conn.execute(
                "UPDATE core_analyses SET document=? WHERE id=?",
                (old.model_validate(document).model_dump_json(), str(old.id)),
            )
        monkeypatch.setattr(core_module, "PROMPT_VERSION", "core-terms-v6")
        if status in ("running", "outcome_pending"):
            with pytest.raises(DomainError):
                core.analyze(project, run, UUID(report), CoreRequest())
            assert len(calls) == 1
        else:
            updated = core.analyze(project, run, UUID(report), CoreRequest())
            assert (updated.id == old.id) is (status == "succeeded")
            assert len(calls) == (1 if status == "succeeded" else 2)
    finally:
        api.__exit__(None, None, None)
