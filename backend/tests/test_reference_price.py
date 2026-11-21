"""用户输入价格须持久化并作为竞对 ±15% 的真实基准，不从图片推算。"""

from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from test_competitors import ready

from app.db.repository import Repository
from app.domain.schemas import ProductInput
from app.main import create_app


def test_price_roundtrip_and_restart(db: str, engine: Engine, tmp_path: Path) -> None:
    with TestClient(create_app(db, storage_root=tmp_path)) as client:
        project = client.post(
            "/api/v1/projects", json={"name": "图片价格", "marketplace": "US"}
        ).json()["id"]
        response = client.post(
            f"/api/v1/projects/{project}/pipeline-runs",
            json={
                "marketplace": "US",
                "image_urls": ["https://example.com/a.jpg"],
                "reference_price": "20.00",
                "reference_currency": "USD",
            },
            headers={"Idempotency-Key": "price"},
        )
        assert response.status_code == 201, response.text
        run = response.json()
    with TestClient(create_app(db, storage_root=tmp_path)) as client:
        saved = client.get(f"/api/v1/projects/{project}/pipeline-runs/{run['id']}").json()
        assert Decimal(saved["input"]["reference_price"]) == Decimal("20")
        materials = client.get(
            f"/api/v1/projects/{project}/pipeline-runs/{run['id']}/product-materials"
        ).json()
        assert Decimal(materials["reference_price"]) == Decimal("20")
        assert materials["reference_currency"] == "USD"


@pytest.mark.parametrize("price", ["0", "-1", "NaN", "Infinity", "1.12345"])
def test_invalid_reference_price(db: str, engine: Engine, price: str) -> None:
    with pytest.raises(ValueError):
        ProductInput(marketplace="US", asin="B012345678", reference_price=price)


def test_competitor_discovery_inherits_price_and_preserves_manual_change(db: str, engine: Engine, tmp_path: Path) -> None:
    client, base = ready(db, engine, tmp_path)
    try:
        repo = Repository(engine, tmp_path)
        run = UUID(base.split("/")[6])
        # 用真实仓库保存一个含价格的输入版本，核心词服务继续使用已确认的真实测试快照。
        import json

        with repo.connect() as conn:
            document = json.loads(
                conn.execute(
                    "SELECT document FROM pipeline_runs WHERE id=?", (str(run),)
                ).fetchone()[0]
            )
            document["input"].update(reference_price="20.00", reference_currency="USD")
            conn.execute(
                "UPDATE pipeline_runs SET document=? WHERE id=?", (json.dumps(document), str(run))
            )
        # API 的发现路径会执行同一 CompetitorService，并在解析 CSV 后立即给出价格状态。
        response = client.post(
            base + "/competitors/discover",
            json={
                "source": "poe_csv",
                "expected_version": 0,
                "source_label": "价格测试",
                "csv_text": "ASIN,Product Name,Average Selling Price (90 days),Currency\nB012345678,Blanket,23,USD\nB087654321,Blanket,23.01,USD",
            },
        )
        assert response.status_code == 200, response.text
        saved = response.json()
        assert Decimal(saved["reference_price"]) == Decimal("20")
        assert [i["price_status"] for i in saved["items"]] == ["within", "outside"]
        reviews = [
            {
                "asin": i["asin"],
                "decision": "include",
                "reason": "测试同款",
                "price_override_reason": "",
            }
            for i in saved["items"]
        ]
        changed = client.put(
            base + "/competitors/selection",
            json={
                "expected_version": saved["version"],
                "reference_price": "25",
                "currency": "USD",
                "tolerance": "0.15",
                "reviews": reviews,
            },
        )
        assert changed.status_code == 200, changed.text
        again = client.post(
            base + "/competitors/discover",
            json={
                "source": "manual",
                "asins": ["B000000003"],
                "expected_version": changed.json()["version"],
            },
        )
        assert again.status_code == 200, again.text
        assert Decimal(again.json()["reference_price"]) == Decimal("25")
    finally:
        client.close()


def test_legacy_input_without_price_remains_readable(db: str, engine: Engine, ) -> None:
    legacy = ProductInput(marketplace="US", image_urls=["https://example.com/a.jpg"])
    assert legacy.reference_price is None
