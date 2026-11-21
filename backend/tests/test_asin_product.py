"""ASIN 资料的身份校验、缓存及纯 ASIN 识别集成。"""

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.domain.errors import DomainError
from app.main import create_app
from app.services.asin_product import ProductSnapshot, parse_product_snapshot
from app.services.qwen import QwenClient, QwenConfig


def test_snapshot_requires_matching_identity_and_title(db: str, engine: Engine, ) -> None:
    data = {
        "asin": "B0FM77JR76",
        "title": "Coffee mug",
        "features": ["Ceramic"],
        "image_url": "https://m.media-amazon.com/a.jpg",
        "blocked": False,
    }
    assert parse_product_snapshot(data, "B0FM77JR76").title == "Coffee mug"
    for changes in [{"asin": "B000000000"}, {"title": ""}, {"blocked": True}]:
        with pytest.raises(DomainError):
            parse_product_snapshot({**data, **changes}, "B0FM77JR76")


def test_asin_only_identification_resolves_and_caches(db: str, engine: Engine, tmp_path: Path) -> None:
    class Provider:
        calls = 0

        def fetch(self, marketplace: str, asin: str) -> ProductSnapshot:
            self.calls += 1
            return ProductSnapshot(
                asin=asin,
                title="Coffee mug",
                features=["Ceramic"],
                image_url="https://m.media-amazon.com/a.jpg",
            )

    provider = Provider()
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert "Coffee mug" in request.content.decode()
        content = json.dumps(
            {"product_name": "咖啡杯", "product_type": "杯子", "features": [], "uncertainties": []}
        )
        return httpx.Response(
            200, json={"choices": [{"finish_reason": "stop", "message": {"content": content}}]}
        )

    model = QwenClient(QwenConfig(api_key="secret"), transport=httpx.MockTransport(handle))
    with TestClient(
        create_app(db, storage_root=tmp_path, model_client=model, product_provider=provider)
    ) as client:
        project = client.post(
            "/api/v1/projects", json={"name": "test", "marketplace": "US"}
        ).json()["id"]
        run = client.post(
            f"/api/v1/projects/{project}/pipeline-runs",
            json={"marketplace": "US", "asin": "B0FM77JR76"},
            headers={"Idempotency-Key": "run"},
        ).json()["id"]
        base = f"/api/v1/projects/{project}/pipeline-runs/{run}"
        first = client.post(base + "/product-understanding")
        assert first.status_code == 200, first.text
        assert client.post(base + "/product-understanding").json() == first.json()
        materials = client.get(base + "/product-materials").json()
        assert materials["asin_resolution"] == "resolved"
        assert materials["asin_product"]["title"] == "Coffee mug"
        assert provider.calls == 1 and len(calls) == 1
        assert (
            client.get(
                base.replace(project, "11111111-1111-4111-8111-111111111111") + "/product-materials"
            ).status_code
            == 404
        )


@pytest.mark.parametrize(
    "code,status",
    [
        ("ASIN_PAGE_NOT_FOUND", 404),
        ("ASIN_ACCESS_BLOCKED", 502),
        ("ASIN_IDENTITY_UNCONFIRMED", 502),
    ],
)
def test_asin_failure_never_calls_model(db: str, engine: Engine, tmp_path: Path, code: str, status: int) -> None:
    class Provider:
        def fetch(self, marketplace: str, asin: str) -> ProductSnapshot:
            raise DomainError(status, code, "无法确认商品详情")

    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500)

    model = QwenClient(QwenConfig(api_key="secret"), transport=httpx.MockTransport(handle))
    with TestClient(
        create_app(db, storage_root=tmp_path, model_client=model, product_provider=Provider())
    ) as client:
        project = client.post(
            "/api/v1/projects", json={"name": "test", "marketplace": "US"}
        ).json()["id"]
        run = client.post(
            f"/api/v1/projects/{project}/pipeline-runs",
            json={"marketplace": "US", "asin": "B0FM77JR76"},
            headers={"Idempotency-Key": "run"},
        ).json()["id"]
        base = f"/api/v1/projects/{project}/pipeline-runs/{run}"
        response = client.post(base + "/product-understanding")
        assert response.status_code == status
        assert response.json()["error"]["code"] == code
        assert client.get(base + "/product-materials").json()["asin_product"] is None
        assert calls == []
