"""自动判断后半段：模型完成清洗分类与流量决定，停在发布门。"""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import Engine
from test_aba import auth_file, row
from test_aba_expansion import prepare, wait
from test_roots import PRODUCT
from test_third_party import CSV, MAPPING

from app.adapters.aba import AbaClient
from app.domain.errors import DomainError
from app.domain.sellersprite import BrowserCollectionInput, BrowserCollectionJob
from app.domain.third_party import ImportReport
from app.domain.workflow import WorkflowView
from app.main import create_app
from app.services.qwen import QwenClient, QwenConfig
from app.workflows import nodes_steps_6_8
from app.workflows.nodes_common import latest_saturday
from app.workflows.service import WorkflowService


def reply(content: object) -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(content)}}]},
    )


def prompt_data(prompt: str) -> dict[str, object]:
    parsed = json.loads(prompt.split("数据：", 1)[1])
    return cast(dict[str, object], parsed) if isinstance(parsed, dict) else {}


def fake_late_model(
    invalid: bool = False,
    calls: list[str] | None = None,
    traffic_hook: Callable[[], None] | None = None,
) -> QwenClient:
    def handle(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        prompt = payload["messages"][0]["content"][0]["text"]
        if "根据已确认商品事实自动分类" in prompt:
            if calls is not None:
                calls.append("cleaning")
            if invalid:
                return reply({"roots": [], "reason": "无法确定"})
            data = prompt_data(prompt)
            frequencies = data.get("frequencies")
            assert isinstance(frequencies, list) and frequencies
            typed_frequencies = cast(list[dict[str, object]], frequencies)
            return reply(
                {
                    "roots": [
                        {
                            "term": item["term"],
                            "category": "preference",
                            "evidence": "结合已确认商品属性判断",
                        }
                        for item in typed_frequencies
                    ],
                    "exclusions": [],
                    "reason": "结合商品事实自动分类",
                }
            )
        if "逐词判断与已确认商品事实的语义相关性" in prompt:
            if traffic_hook is not None:
                traffic_hook()
            data = prompt_data(prompt)
            rows = data.get("rows")
            assert isinstance(rows, list) and rows
            items: list[dict[str, object]] = []
            for raw in cast(list[object], rows):
                if not isinstance(raw, dict):
                    continue
                item = cast(dict[str, object], raw)
                items.append(
                    {
                        "keyword": item["keyword"],
                        "decision": "keep",
                        "reason": "与商品相关",
                        "evidence_note": "词义匹配",
                    }
                )
            return reply({"items": items})
        return reply(PRODUCT)

    return QwenClient(QwenConfig(api_key="secret"), transport=httpx.MockTransport(handle))


def preview_period(
    api: TestClient,
    url: str,
    query: str,
    direction: str,
) -> dict[str, object]:
    result = api.post(
        url + "/preview",
        files={"file": ("seller.csv", CSV, "text/csv")},
        data={
            "metadata": json.dumps(
                {
                    "direction": direction,
                    "query": query,
                    "marketplace": "US",
                    "period_kind": "last_30_days",
                    "collected_at": datetime.now(UTC).isoformat(),
                    "source_total": 3,
                    "export_complete": True,
                }
            )
        },
    )
    assert result.status_code == 200, result.text
    return result.json()


@pytest.mark.parametrize(
    "mode", ["auto", "invalid", "manual", "concurrent", "retry", "stale_import", "stale_job"]
)
def test_auto_cleaning_and_traffic_reach_publish_gate(
    db: str, engine: Engine, tmp_path: Path, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    base = prepare(db, engine, tmp_path)
    week = latest_saturday()
    aba = AbaClient(
        auth_file(tmp_path),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "reportsV2": [
                        {"totalItems": 1, "rows": [row("portable cooling blankets", 200001)]}
                    ]
                },
            )
        ),
    )
    inserted: list[str] = []
    traffic_calls: list[int] = []
    if mode == "retry":
        monkeypatch.setattr(nodes_steps_6_8, "TRAFFIC_BATCH", 1)

    def concurrent_review() -> None:
        if mode == "retry":
            traffic_calls.append(1)
            if len(traffic_calls) == 2:
                raise DomainError(504, "MODEL_OUTCOME_UNKNOWN", "模拟中断")
        if mode != "concurrent" or inserted:
            return
        report = api.get(base + "/traffic").json()[0]
        rows = api.get(base + f"/traffic/{report['id']}/rows?limit=200").json()["items"]
        keyword = next(r["phrase"]["keyword"] for r in rows if not r["cleaning_exclusion"])
        saved = api.put(
            base + f"/traffic/{report['id']}/review",
            json={
                "expected_version": report["version"],
                "keyword": keyword,
                "decision": "exclude",
                "priority": "low",
                "reason": "人工核对排除",
                "evidence_note": "人工证据",
                "reviewer": "人工运营",
            },
        )
        assert saved.status_code == 200, saved.text
        inserted.append(keyword)

    with TestClient(
        create_app(
            db,
            aba_client=aba,
            model_client=fake_late_model(mode == "invalid", calls, concurrent_review),
            workflow_checkpointer=InMemorySaver(),
            workflow_inline=True,
        )
    ) as api:
        jobs: list[BrowserCollectionJob] = []
        deps = cast(WorkflowService, cast(FastAPI, api.app).state.workflow_service).deps
        for direction in ("forward", "reverse"):
            task = api.post(
                base + "/aba-expansion",
                json={"direction": direction, "week_end": week.isoformat(), "max_rows": 100},
            ).json()
            assert wait(api, base + "/aba-expansion", task["id"])["status"] == "succeeded"
        for direction, query in [
            ("forward", "cooling blanket"),
            ("reverse", "B012345678"),
            ("reverse", "B087654321"),
        ]:
            imported = preview_period(
                api,
                base + "/third-party",
                query,
                direction,
            )
            if mode in {"stale_import", "stale_job"}:
                report = deps.third_party.get(
                    UUID(str(imported["project_id"])),
                    UUID(str(imported["run_id"])),
                    UUID(str(imported["id"])),
                )
                now = datetime.now(UTC)
                jobs.append(
                    BrowserCollectionJob(
                        id=uuid4(),
                        project_id=report.project_id,
                        run_id=report.run_id,
                        core_selection_id=report.core_selection_id,
                        competitor_id=report.competitor_id,
                        request=BrowserCollectionInput(
                            direction=report.metadata.direction, query=report.metadata.query
                        ),
                        status="succeeded",
                        report_id=report.id,
                        created_at=now,
                        updated_at=now,
                    )
                )
                continue
            confirmed = api.post(
                base + f"/third-party/{imported['id']}/confirm",
                json={"mapping": MAPPING, "share_unit": "percent"},
            )
            assert confirmed.status_code == 200, confirmed.text

        if mode in {"stale_import", "stale_job"}:
            old_id = uuid4()
            old = jobs[0].model_copy(
                update={
                    "id": uuid4(),
                    "report_id": old_id,
                    "core_selection_id": uuid4()
                    if mode == "stale_job"
                    else jobs[0].core_selection_id,
                }
            )
            read_report = deps.third_party.get

            def read_with_history(project: UUID, run: UUID, key: UUID) -> ImportReport:
                if key == old_id:
                    if mode == "stale_job":
                        raise AssertionError("must not read report of old-context job")
                    return read_report(project, run, cast(UUID, jobs[0].report_id)).model_copy(
                        update={"id": old_id, "stale": True}
                    )
                return read_report(project, run, key)

            monkeypatch.setattr(deps.third_party, "get", read_with_history)

            def list_jobs(_project: UUID, _run: UUID) -> list[BrowserCollectionJob]:
                return [old, *jobs]

            monkeypatch.setattr(deps.sellersprite, "list", list_jobs)

        started = api.post(
            base + "/workflow",
            json={"options": {"auto_decide": True, "manual_cleaning_review": mode == "manual"}},
        )
        assert started.status_code == 202, started.text
        view = WorkflowView.model_validate(started.json())
        if mode == "retry":
            assert view.status == "failed"
            saved = api.get(base + "/traffic").json()[0]
            # 并发审核中断时，其他已在途批次仍可能先完成；只要求保留部分进度。
            assert 0 < saved["reviewed"] < saved["required"]
            retried = api.post(
                base + f"/workflow/{view.workflow_id}/retry-traffic",
                json={
                    "expected_revision": view.revision,
                    "gate": "traffic_review",
                    "action": "continue",
                },
            )
            assert retried.status_code == 202, retried.text
            view = WorkflowView.model_validate(retried.json())
            audits = api.get(base + f"/traffic/{saved['id']}/reviews").json()
            assert len(audits) == len({r["data"]["keyword"] for r in audits})
            assert len(traffic_calls) == saved["required"] + 1
        if mode == "invalid":
            assert view.status == "failed"
            assert view.waiting_for is None
            assert len(calls) == 2
            cleaning = api.get(base + "/cleaning").json()[0]
            assert cleaning["review"] is None
            return
        assert view.waiting_for is not None
        if mode == "manual":
            assert len(calls) == 1
        assert view.waiting_for.gate == "library_publish"
        statuses = {node.key: node.status for node in view.nodes}
        assert statuses["cleaning_review"] in ("done", "skipped")
        assert statuses["traffic_review"] == "done"
        assert statuses["create_library"] == "done"
        assert statuses["library_publish"] == "waiting_human"
        if mode == "concurrent":
            report = api.get(base + "/traffic").json()[0]
            audits = api.get(base + f"/traffic/{report['id']}/reviews").json()
            assert len({r["data"]["keyword"] for r in audits}) == len(audits)
            human = [r for r in audits if r["data"]["keyword"] == inserted[0]]
            assert len(human) == 1 and human[0]["data"]["reviewer"] == "人工运营"
