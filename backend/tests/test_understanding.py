"""识别 API 的持久化、输入边界与项目隔离。"""

import json
from io import BytesIO
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import Engine

from app.domain.asin_product import ProductSnapshot
from app.domain.errors import DomainError
from app.main import create_app
from app.services.qwen import QwenClient, QwenConfig


class UnavailableProductProvider:
    def fetch(self, marketplace: str, asin: str) -> ProductSnapshot:
        raise DomainError(502, "ASIN_ACCESS_FAILED", "商品页不可用")


class FixedProductProvider:
    def fetch(self, marketplace: str, asin: str) -> ProductSnapshot:
        return ProductSnapshot(asin=asin, title="杯子")


def test_identification_persists_and_does_not_charge_twice(db: str, engine: Engine, tmp_path: Path) -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {
                                    "product_name": "商品",
                                    "product_type": "收纳架",
                                    "features": [],
                                    "uncertainties": ["尺寸未知"],
                                }
                            )
                        },
                    }
                ]
            },
        )

    model = QwenClient(QwenConfig(api_key="secret"), transport=httpx.MockTransport(handle))
    with TestClient(
        create_app(db, storage_root=tmp_path, model_client=model, product_provider=UnavailableProductProvider())
    ) as client:
        project = client.post(
            "/api/v1/projects", json={"name": "识别", "marketplace": "US"}
        ).json()["id"]
        buffer = BytesIO()
        Image.new("RGB", (10, 10)).save(buffer, format="PNG")
        asset = client.post(
            f"/api/v1/projects/{project}/images",
            headers={"Idempotency-Key": "image"},
            files={"file": ("p.png", buffer.getvalue(), "image/png")},
        ).json()
        run = client.post(
            f"/api/v1/projects/{project}/pipeline-runs",
            headers={"Idempotency-Key": "run"},
            json={"marketplace": "US", "image_ids": [asset["id"]]},
        ).json()
        url = f"/api/v1/projects/{project}/pipeline-runs/{run['id']}/product-understanding"
        assert client.get(url).status_code == 404
        first = client.post(url)
        assert first.status_code == 200, first.text
        assert first.json()["model"] == "qwen3.8-max"
        assert (
            client.get(url.replace("/product-understanding", "/product-materials")).json()[
                "model_status"
            ]
            == "succeeded"
        )
        assert client.post(url).json() == first.json()
        assert len(calls) == 1
        other = client.post("/api/v1/projects", json={"name": "其他", "marketplace": "US"}).json()[
            "id"
        ]
        assert client.post(url.replace(project, other)).status_code == 404
        assert len(calls) == 1
        asin = client.post(
            f"/api/v1/projects/{project}/pipeline-runs",
            headers={"Idempotency-Key": "asin"},
            json={"marketplace": "US", "asin": "B0ABCDEF12"},
        ).json()
        rejected = client.post(url.replace(run["id"], asin["id"]))
        assert rejected.status_code == 502
        assert rejected.json()["error"]["code"] == "ASIN_ACCESS_FAILED"
        assert len(calls) == 1
    with TestClient(
        create_app(db, storage_root=tmp_path, model_client=model, product_provider=UnavailableProductProvider())
    ) as client:
        assert client.get(url).json() == first.json()
        assert client.post(url).json() == first.json()
        assert len(calls) == 1


def test_timeout_and_running_claim_prevent_duplicate_call(db: str, engine: Engine, tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    import pytest

    from app.db.repository import Repository
    from app.domain.errors import DomainError
    from app.domain.schemas import ProductInput, ProjectCreate
    from app.services.asin_product import AsinProductService
    from app.services.images import ImageService
    from app.services.product_understanding import ProductUnderstandingService

    repository = Repository(engine, tmp_path)
    repository.initialize()
    project = repository.create_project(ProjectCreate(name="并发", marketplace="US"))
    run = repository.create_run(
        project.id, ProductInput(marketplace="US", asin="B0ABCDEF12", description="杯子"), "run"
    )
    started, release = Event(), Event()
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        started.set()
        assert release.wait(10)
        raise httpx.ReadTimeout("private", request=request)

    service = ProductUnderstandingService(
        repository,
        ImageService(repository, tmp_path / "uploads"),
        QwenClient(QwenConfig(api_key="secret"), transport=httpx.MockTransport(handle)),
        AsinProductService(repository, FixedProductProvider()),
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(service.identify, project.id, run.id)
        assert started.wait(10)
        try:
            with pytest.raises(DomainError) as error:
                service.identify(project.id, run.id)
            assert error.value.code == "MODEL_OUTCOME_PENDING"
        finally:
            release.set()
        with pytest.raises(DomainError) as error:
            pending.result(timeout=10)
        assert error.value.code == "MODEL_OUTCOME_UNKNOWN"
    # 断线后重试也不应再次付费，服务重启后规则相同。
    with pytest.raises(DomainError) as error:
        service.identify(project.id, run.id)
    assert error.value.code == "MODEL_OUTCOME_PENDING"
    assert len(calls) == 1
