"""编排图：启动后依次停在人工门，人在现有接口完成动作后 resume 前进。"""

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import Engine
from test_aba import auth_file, row
from test_roots import CANDIDATES, PRODUCT, FixedProductProvider

from app.adapters.aba import AbaClient
from app.db.repository import Repository
from app.domain.workflow import WorkflowView
from app.main import create_app
from app.services.qwen import QwenClient, QwenConfig


def fake_model() -> QwenClient:
    calls: list[int] = []

    def handle(request: httpx.Request) -> httpx.Response:
        prompt = json.loads(request.content)["messages"][0]["content"][0]["text"]
        calls.append(1)
        content = (
            PRODUCT
            if len(calls) == 1
            else {
                "purpose": ["饮用"],
                "style_structure": ["带手柄"],
                "specifications": ["杯子"],
                "package_quantity": "1",
                "unit_basis": "each",
            }
            if "提取商品四维基准" in prompt
            else CANDIDATES
        )
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(content)}}]
            },
        )

    return QwenClient(QwenConfig(api_key="secret"), transport=httpx.MockTransport(handle))


def fake_aba(tmp_path: Path) -> AbaClient:
    return AbaClient(
        auth_file(tmp_path),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, json={"reportsV2": [{"totalItems": 1, "rows": [row("mug", 5000)]}]}
            )
        ),
    )


@pytest.mark.parametrize("legacy", [False, True])
def test_workflow_walks_human_gates_in_order(db: str, engine: Engine, tmp_path: Path, legacy: bool) -> None:
    with TestClient(
        create_app(
            db,
            model_client=fake_model(),
            aba_client=fake_aba(tmp_path),
            workflow_checkpointer=InMemorySaver(),
            workflow_inline=True,
            product_provider=FixedProductProvider(),
        )
    ) as api:
        project = api.post("/api/v1/projects", json={"name": "杯子", "marketplace": "US"}).json()[
            "id"
        ]
        run = api.post(
            f"/api/v1/projects/{project}/pipeline-runs",
            json={"marketplace": "US", "asin": "B0ABCDEF12", "description": "红色杯子"},
            headers={"Idempotency-Key": "run"},
        ).json()
        base = f"/api/v1/projects/{project}/pipeline-runs/{run['id']}"
        started = api.post(base + "/workflow", json={"options": {"auto_decide": False}})
        assert started.status_code == 202, started.text
        view = WorkflowView.model_validate(started.json())
        assert view.status == "waiting_human"
        assert view.waiting_for is not None
        assert view.waiting_for.gate == "product_confirmation"
        assert view.current_node == "product_confirmation"

        def resume(gate: str, revision: int, payload: dict[str, str]) -> WorkflowView:
            response = api.post(
                base + f"/workflow/{view.workflow_id}/resume",
                json={
                    "gate": gate,
                    "action": "continue",
                    "expected_revision": revision,
                    "payload": payload,
                },
            )
            assert response.status_code == 202, response.text
            return WorkflowView.model_validate(response.json())

        conflict = api.post(
            base + f"/workflow/{view.workflow_id}/resume",
            json={
                "gate": "product_confirmation",
                "action": "continue",
                "expected_revision": view.revision + 1,
                "payload": {},
            },
        )
        assert conflict.status_code == 409
        mismatch = api.post(
            base + f"/workflow/{view.workflow_id}/resume",
            json={
                "gate": "root_selection",
                "action": "continue",
                "expected_revision": view.revision,
                "payload": {},
            },
        )
        assert mismatch.status_code == 409

        product = api.get(base + "/product-understanding").json()["product"]
        confirmed = api.put(
            base + "/product-confirmation",
            json={
                "expected_version": 0,
                "product": product,
                "reviewer": "本地运营",
                "reason": "核对商品资料",
            },
        )
        assert confirmed.status_code == 200, confirmed.text
        view = resume("product_confirmation", view.revision, {})
        assert view.waiting_for is not None
        assert view.waiting_for.gate == "product_profile"
        draft = api.get(base + "/product-profile").json()
        profile = draft["profile"]
        saved_profile = api.put(
            base + "/product-profile",
            json={
                "expected_version": draft["version"],
                "purpose": profile["purpose"],
                "style_structure": profile["style_structure"],
                "specifications": profile["specifications"],
                "package_quantity": profile["package_quantity"],
                "unit_basis": profile["unit_basis"],
                "currency": profile["currency"],
            },
        )
        assert saved_profile.status_code == 200, saved_profile.text
        view = resume("product_profile", view.revision, {})
        assert view.waiting_for is not None
        assert view.waiting_for.gate == "root_selection"

        candidates = api.get(base + "/root-candidates").json()["candidates"]
        if legacy:
            with Repository(engine, tmp_path).connect() as conn:
                document = json.loads(
                    conn.execute(
                        "SELECT document FROM root_candidates WHERE run_id=?", (run["id"],)
                    ).fetchone()[0]
                )
                document.pop("rule_version")
                conn.execute(
                    "UPDATE root_candidates SET document=? WHERE run_id=?",
                    (json.dumps(document), run["id"]),
                )
        assert (
            api.put(
                base + "/root-selection", json={"candidate_ids": [candidates[0]["id"]]}
            ).status_code
            == 200
        )
        view = resume("root_selection", view.revision, {})
        assert view.waiting_for is not None
        if legacy:
            assert view.waiting_for.gate == "week"
            view = resume("week", view.revision, {"aba_week_end": "2026-10-17"})
            assert view.waiting_for is not None
        assert view.waiting_for.gate == "core_terms"
        statuses = {node.key: node.status for node in view.nodes}
        assert statuses["collect_core_report"] == "done"
        assert statuses["core_terms"] == "waiting_human"
        assert view.revision > 0
