"""输入接口回归测试：组合校验、幂等重试、持久化与项目归属查询。"""

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

import app.main as main_module
from app.db.engine import build_engine
from app.main import create_app


@pytest.fixture
def client(db: str, tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(db, storage_root=tmp_path)) as instance:
        yield instance


def test_application_shutdown_disposes_its_database_engine(
    db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owned_engine = build_engine(db)
    dispose = Mock(wraps=owned_engine.dispose)
    monkeypatch.setattr(owned_engine, "dispose", dispose)

    def same_engine(_url: str) -> Engine:
        return owned_engine

    monkeypatch.setattr(main_module, "build_engine", same_engine)

    with TestClient(create_app(db, storage_root=tmp_path)):
        pass

    dispose.assert_called_once_with()


def project(client: TestClient) -> str:
    result = client.post("/api/v1/projects", json={"name": "测试商品", "marketplace": "US"})
    assert result.status_code == 201
    return str(result.json()["id"])


@pytest.mark.parametrize(
    "inputs",
    [
        {"asin": "b0abcdef12"},
        {"image_urls": ["https://example.com/main.jpg"]},
        {
            "product_name": "哑铃架",
            "asin": "B0ABCDEF12",
            "image_urls": ["https://example.com/main.jpg"],
        },
    ],
)
def test_accepts_input_combinations(db: str, engine: Engine, client: TestClient, inputs: dict[str, object]) -> None:
    project_id = project(client)
    result = client.post(
        f"/api/v1/projects/{project_id}/pipeline-runs",
        json={"marketplace": "US", **inputs},
        headers={"Idempotency-Key": "first"},
    )
    assert result.status_code == 201
    body = result.json()
    assert body["status"] == "input_received"
    assert body["version"] == 1
    assert body["project_id"] == project_id
    assert result.headers["location"].endswith(body["id"])
    assert result.headers["etag"] == '"1"'
    if "asin" in inputs:
        assert body["input"]["asin"] == "B0ABCDEF12"


@pytest.mark.parametrize(
    "inputs",
    [
        {"product_name": "哑铃架"},
        {"marketplace": "US"},
        {"marketplace": "US", "product_name": "哑铃架"},
        {"marketplace": "US", "description": "已知词：dumbbell rack"},
        {"marketplace": "US", "product_name": "   "},
        {"marketplace": "XX", "asin": "B0ABCDEF12"},
        {"marketplace": "US", "asin": "bad"},
        {"marketplace": "US", "image_urls": ["file:///C:/private.jpg"]},
        {"marketplace": "US", "image_urls": ["https://example.com/a.jpg"] * 6},
        {"marketplace": "US", "product_name": "哑铃架", "unknown_field": True},
    ],
)
def test_rejects_invalid_input(db: str, engine: Engine, client: TestClient, inputs: dict[str, object]) -> None:
    result = client.post(
        f"/api/v1/projects/{project(client)}/pipeline-runs",
        json=inputs,
        headers={"Idempotency-Key": "invalid"},
    )
    assert result.status_code == 422
    assert result.json()["error"]["code"] == "VALIDATION_ERROR"
    assert result.json()["error"]["request_id"] == result.headers["x-request-id"]


def test_idempotency_and_site_guard(db: str, engine: Engine, client: TestClient) -> None:
    url = f"/api/v1/projects/{project(client)}/pipeline-runs"
    payload = {"marketplace": "US", "product_name": "哑铃架", "asin": "B0ABCDEF12"}
    headers = {"Idempotency-Key": "stable"}
    first = client.post(url, json=payload, headers=headers)
    replay = client.post(url, json=payload, headers=headers)
    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json()
    assert client.post(url, json=payload).status_code == 422
    assert (
        client.post(url, json={**payload, "product_name": "椅子"}, headers=headers).status_code
        == 409
    )
    assert (
        client.post(
            url, json={**payload, "marketplace": "CA"}, headers={"Idempotency-Key": "other"}
        ).status_code
        == 422
    )


def test_only_us_marketplace_is_supported(db: str, engine: Engine, client: TestClient) -> None:
    rejected = client.post("/api/v1/projects", json={"name": "德语项目", "marketplace": "DE"})
    assert rejected.status_code == 422
    assert "美国站" in rejected.text
    project_id = project(client)
    url = f"/api/v1/projects/{project_id}/pipeline-runs"
    non_us = client.post(
        url,
        json={"marketplace": "de", "asin": "B0ABCDEF12"},
        headers={"Idempotency-Key": "non-us"},
    )
    assert non_us.status_code == 422
    assert "美国站" in non_us.text
    accepted = client.post(
        url,
        json={"marketplace": "us", "asin": "B0ABCDEF12"},
        headers={"Idempotency-Key": "lowercase-us"},
    )
    assert accepted.status_code == 201
    assert accepted.json()["input"]["marketplace"] == "US"


def test_persistence_and_project_isolation(db: str, engine: Engine, tmp_path: Path) -> None:
    with TestClient(create_app(db, storage_root=tmp_path)) as client:
        project_id = project(client)
        url = f"/api/v1/projects/{project_id}/pipeline-runs"
        created = client.post(
            url,
            json={"marketplace": "US", "product_name": "哑铃架", "asin": "B0ABCDEF12"},
            headers={"Idempotency-Key": "saved"},
        )
        assert created.status_code == 201
        run_id = created.json()["id"]
    with TestClient(create_app(db, storage_root=tmp_path)) as client:
        result = client.get(f"{url}/{run_id}")
        assert result.status_code == 200
        assert result.json() == created.json()
        other_id = project(client)
        assert client.get(f"/api/v1/projects/{other_id}/pipeline-runs/{run_id}").status_code == 404
        assert client.get(f"/api/v1/projects/{project_id}").status_code == 200


def test_health_and_missing_project(db: str, engine: Engine, client: TestClient) -> None:
    assert client.get("/api/v1/health/live").status_code == 200
    assert client.get("/api/v1/health/ready").status_code == 200
    result = client.post(
        "/api/v1/projects/00000000-0000-0000-0000-000000000000/pipeline-runs",
        json={"marketplace": "US", "product_name": "哑铃架", "asin": "B0ABCDEF12"},
        headers={"Idempotency-Key": "missing"},
    )
    assert result.status_code == 404
    assert result.json()["error"]["code"] == "NOT_FOUND"
