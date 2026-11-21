"""词根推荐与确认：模型契约、项目隔离、结果复用和人工选择持久化。"""

import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.domain.asin_product import ProductSnapshot
from app.domain.errors import DomainError
from app.domain.product_understanding import ProductUnderstanding
from app.main import create_app
from app.services.qwen import QwenClient, QwenConfig

PRODUCT = {
    "product_name": "红色杯子",
    "product_type": "马克杯",
    "features": ["带手柄"],
    "uncertainties": ["材质未知"],
}
CANDIDATES = {
    "candidates": [
        {"text": "mug", "translation": "马克杯", "reason": "对应已识别商品类型"},
        {"text": "coffee mug", "translation": "咖啡杯", "reason": "常见商品称呼，仍需搜索验证"},
    ]
}


class FixedProductProvider:
    def fetch(self, marketplace: str, asin: str) -> ProductSnapshot:
        return ProductSnapshot(
            asin=asin, title="红色杯子", price=Decimal("20"), currency="USD"
        )


def test_root_api_cache_selection_and_project_scope(db: str, engine: Engine, tmp_path: Path) -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        content = PRODUCT if len(calls) == 1 else CANDIDATES
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(content)}}]
            },
        )

    model = QwenClient(QwenConfig(api_key="secret"), transport=httpx.MockTransport(handle))
    with TestClient(create_app(db, storage_root=tmp_path, model_client=model, product_provider=FixedProductProvider())) as client:
        project = client.post(
            "/api/v1/projects", json={"name": "杯子", "marketplace": "US"}
        ).json()["id"]
        run = client.post(
            f"/api/v1/projects/{project}/pipeline-runs",
            json={"marketplace": "US", "asin": "B0ABCDEF12", "description": "红色杯子"},
            headers={"Idempotency-Key": "run"},
        ).json()
        base = f"/api/v1/projects/{project}/pipeline-runs/{run['id']}"
        url = base + "/root-candidates"
        assert client.post(url).status_code == 409
        assert calls == []
        assert client.post(base + "/product-understanding").status_code == 200
        response = client.post(url)
        assert response.status_code == 200, response.text
        result = response.json()
        assert len(result["candidates"]) == 2
        assert result["language"] == "en"
        assert client.post(url).json() == result
        assert len(calls) == 2
        selection = base + "/root-selection"
        ids = [x["id"] for x in result["candidates"]]
        assert client.get(selection).status_code == 404
        for invalid in [[], [ids[0], ids[0]], ["00000000-0000-0000-0000-000000000000"]]:
            assert client.put(selection, json={"candidate_ids": invalid}).status_code == 422
        saved = client.put(selection, json={"candidate_ids": [ids[0]]})
        assert saved.status_code == 422  # 未验证候选不能通过人工选择绕过门槛。
        from uuid import UUID, uuid4

        from app.db.repository import Repository
        from app.domain.product_terms import ProductTermAnalysis, TermAnalysisItem, TermSummary
        analysis = ProductTermAnalysis(
            id=uuid4(), project_id=UUID(project), run_id=UUID(run["id"]), profile_version=1,
            status="completed", complete=True,
            items=[TermAnalysisItem(candidate_id=UUID(ids[0]), text="mug", status="completed", summary=TermSummary(valid_samples=30, high_related_count=20, relevance_percent=Decimal("66.67"), confidence="high"))],
            best_candidate_ids=[UUID(ids[0])],
        )
        with Repository(engine, tmp_path).connect() as conn:
            conn.execute("INSERT INTO product_term_analyses(id,run_id,fingerprint,document) VALUES(?,?,?,?)", (str(analysis.id), run["id"], "root-api-validation", analysis.model_dump_json()))
        saved = client.put(selection, json={"candidate_ids": [ids[0]]})
        assert saved.status_code == 200
        assert saved.json()["candidate_ids"] == [ids[0]]
        assert client.put(selection, json={"candidate_ids": [ids[0]]}).json() == saved.json()
        other = client.post("/api/v1/projects", json={"name": "其他", "marketplace": "US"}).json()[
            "id"
        ]
        assert client.post(url.replace(project, other)).status_code == 404
        assert (
            client.put(
                selection.replace(project, other), json={"candidate_ids": [ids[0]]}
            ).status_code
            == 404
        )
        assert len(calls) == 2
    with TestClient(create_app(db, storage_root=tmp_path, model_client=model, product_provider=FixedProductProvider())) as client:
        assert client.get(url).json() == result
        assert client.get(selection).json() == saved.json()
        assert client.post(url).json() == result
        assert len(calls) == 2


def test_qwen_roots_use_us_language_and_deduplicate(db: str, engine: Engine, ) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        prompt = json.loads(request.content)["messages"][0]["content"][0]["text"]
        assert "语言为 en" in prompt and "材质未知" in prompt
        duplicate = {
            "candidates": [
                CANDIDATES["candidates"][0],
                {**CANDIDATES["candidates"][0], "text": "  MUG  "},
            ]
        }
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"finish_reason": "stop", "message": {"content": json.dumps(duplicate)}}
                ]
            },
        )

    client = QwenClient(QwenConfig(api_key="secret"), transport=httpx.MockTransport(handle))
    result = client.recommend_roots("US", ProductUnderstanding.model_validate(PRODUCT))
    assert len(result.candidates) == 1


def test_invalid_root_output_is_rejected(db: str, engine: Engine, ) -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": '{"candidates":[]}'}}]
            },
        )
    )
    client = QwenClient(QwenConfig(api_key="secret"), transport=transport)
    with pytest.raises(DomainError) as error:
        client.recommend_roots("US", ProductUnderstanding.model_validate(PRODUCT))
    assert error.value.code == "MODEL_OUTPUT_INVALID"


def test_root_timeout_blocks_repeat_paid_calls(db: str, engine: Engine, tmp_path: Path) -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise httpx.ReadTimeout("private upstream detail", request=request)
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(PRODUCT)}}]
            },
        )

    model = QwenClient(QwenConfig(api_key="secret"), transport=httpx.MockTransport(handle))
    with TestClient(create_app(db, storage_root=tmp_path, model_client=model, product_provider=FixedProductProvider())) as client:
        project = client.post(
            "/api/v1/projects", json={"name": "杯子", "marketplace": "US"}
        ).json()["id"]
        run = client.post(
            f"/api/v1/projects/{project}/pipeline-runs",
            json={"marketplace": "US", "asin": "B0ABCDEF12", "description": "红杯"},
            headers={"Idempotency-Key": "timeout"},
        ).json()["id"]
        base = f"/api/v1/projects/{project}/pipeline-runs/{run}"
        assert client.post(base + "/product-understanding").status_code == 200
        failed = client.post(base + "/root-candidates")
        assert failed.status_code == 504
        assert "private" not in failed.text and "secret" not in failed.text
        assert client.post(base + "/root-candidates").status_code == 409
        assert calls == 2
    with TestClient(create_app(db, storage_root=tmp_path, model_client=model, product_provider=FixedProductProvider())) as client:
        assert client.post(base + "/root-candidates").status_code == 409
        assert calls == 2


def test_product_term_request_uses_searchable_minimum_phrase(db: str, engine: Engine, ) -> None:
    """验证新商品词规则；多词固定名称仍可作为最小可搜索词组。"""

    def handle(request: httpx.Request) -> httpx.Response:
        prompt = json.loads(request.content)["messages"][0]["content"][0]["text"]
        assert "能搜索到同类产品的单词或词组" in prompt
        assert "优先准确表达完整商品类别" in prompt
        assert "不要为了凑数量" in prompt
        assert "expansions" not in prompt
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {
                                    "candidates": [
                                        {
                                            "text": "ice cream",
                                            "translation": "冰淇淋",
                                            "reason": "固定品类名称，拆成 ice 或 cream 均改变商品含义",
                                        }
                                    ]
                                }
                            )
                        },
                    }
                ]
            },
        )

    client = QwenClient(QwenConfig(api_key="secret"), transport=httpx.MockTransport(handle))
    product = ProductUnderstanding.model_validate({**PRODUCT, "product_type": "冰淇淋"})
    result = client.recommend_roots("US", product)
    assert [item.text for item in result.candidates] == ["ice cream"]
