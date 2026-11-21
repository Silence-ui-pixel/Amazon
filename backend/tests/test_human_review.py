"""人工纠错、批量审核和异常发布的端到端契约。"""

import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from test_library import ready_library
from test_roots import CANDIDATES, PRODUCT, FixedProductProvider

from app.main import create_app
from app.services.qwen import QwenClient, QwenConfig


def test_product_confirmation_keeps_original_and_locks_after_generation(db: str, engine: Engine, tmp_path: Path) -> None:
    prompts: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        prompts.append(request.content.decode())
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(PRODUCT if len(prompts) == 1 else CANDIDATES)
                        },
                    }
                ]
            },
        )

    with TestClient(
        create_app(
            db,
            storage_root=tmp_path,
            model_client=QwenClient(
                QwenConfig(api_key="test"), transport=httpx.MockTransport(handle)
            ),
            product_provider=FixedProductProvider(),
        )
    ) as api:
        project = api.post(
            "/api/v1/projects", json={"name": "商品确认", "marketplace": "US"}
        ).json()["id"]
        run = api.post(
            f"/api/v1/projects/{project}/pipeline-runs",
            json={"marketplace": "US", "asin": "B0ABCDEF12", "description": "杯子"},
            headers={"Idempotency-Key": "review"},
        ).json()
        base = f"/api/v1/projects/{project}/pipeline-runs/{run['id']}"
        original = api.post(base + "/product-understanding").json()
        confirmation = {
            "expected_version": 0,
            "product": {**PRODUCT, "product_type": "陶瓷旅行杯", "features": ["保温"]},
            "reviewer": "运营",
            "reason": "图片人工核对",
        }
        response = api.put(base + "/product-confirmation", json=confirmation)
        assert response.status_code == 200, response.text
        assert response.json()["version"] == 1
        assert api.get(base + "/product-understanding").json() == original
        assert api.put(base + "/product-confirmation", json=confirmation).status_code == 409
        assert api.post(base + "/root-candidates").status_code == 200
        assert "陶瓷旅行杯" in prompts[-1]
        from uuid import UUID

        from app.adapters.aba import AbaClient
        from app.db.repository import Repository
        from app.services.aba import AbaService
        from app.services.core_terms import CoreService

        repository = Repository(engine, tmp_path)
        core = CoreService(
            repository, AbaService(repository, AbaClient(tmp_path / "unused.json")), None
        )
        assert core._product(UUID(run["id"])).product.product_type == "陶瓷旅行杯"  # pyright: ignore[reportPrivateUsage]
        assert (
            api.put(
                base + "/product-confirmation", json={**confirmation, "expected_version": 1}
            ).status_code
            == 409
        )
        assert len(api.get(base + "/product-confirmation/history").json()) == 1


def test_batch_review_atomicity_and_preserves_evidence(db: str, engine: Engine, tmp_path: Path) -> None:
    with ready_library(db, engine, tmp_path) as (api, base, _database_url, source):
        url = base + f"/traffic/{source['traffic_id']}"
        evidence_review = {
            "expected_version": 2,
            "keyword": "cooling blanket",
            "decision": "keep",
            "priority": "high",
            "style_similar": True,
            "price_delta_ratio": "0.10",
            "first_half_result_share": "0.70",
            "reason": "单词核对",
            "reviewer": "逐词审核",
            "evidence_note": "此词独有的页面价格证据",
        }
        assert api.put(url + "/review", json=evidence_review).status_code == 200
        before = api.get(url + "/rows").json()["items"]
        batch = {
            "expected_version": 3,
            "keywords": ["cooling blanket", "portable cooling blankets"],
            "decision": "exclude",
            "priority": "low",
            "reason": "不纳入本轮",
            "reviewer": "批量运营",
            "evidence_note": "人工逐词浏览确认",
        }
        assert (
            api.put(
                url + "/batch-review", json={**batch, "keywords": ["cooling blanket", "=1+1"]}
            ).status_code
            == 422
        )
        assert api.get(url).json()["version"] == 3
        assert (
            api.put(url + "/batch-review", json={**batch, "reviewer": "甲" * 101}).status_code
            == 422
        )
        response = api.put(url + "/batch-review", json=batch)
        assert response.status_code == 200, response.text
        assert response.json()["version"] == 5
        after = api.get(url + "/rows").json()["items"]
        for row in after:
            if row["cleaning_exclusion"]:
                continue
            old = next(r for r in before if r["phrase"]["keyword"] == row["phrase"]["keyword"])
            assert row["review"]["relevance"] == old["review"]["relevance"]
            assert row["review"]["data"]["evidence_note"].startswith(
                old["review"]["data"]["evidence_note"]
            )
            assert row["review"]["data"]["decision"] == "exclude"
        assert api.put(url + "/batch-review", json=batch).status_code == 409
        assert len(api.get(url + "/reviews").json()) == 5
        relevant = next(r for r in after if r["phrase"]["keyword"] == "cooling blanket")
        assert relevant["review"]["relevance"] == "high"
        assert relevant["review"]["data"]["price_delta_ratio"] == "0.10"


def test_publication_does_not_require_or_retain_exception_acknowledgements(
    db: str, engine: Engine, tmp_path: Path
) -> None:
    with ready_library(db, engine, tmp_path) as (api, base, _database_url, source):
        draft = api.post(base + "/library", json=source).json()
        url = base + f"/library/{draft['id']}/publish"
        publish = {
            "expected_version": 0,
            "publisher": "运营",
            "reason": "确认发布",
            "confirmed": True,
        }
        response = api.post(url, json=publish)
        assert response.status_code == 200, response.text
        assert "exception_reviews" not in response.json()["publication"]
