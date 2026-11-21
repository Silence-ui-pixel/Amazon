"""商品资料接口回归：来源、未接入状态、文件缺失与项目归属。"""

from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import Engine

from app.main import create_app


def create_project(client: TestClient) -> str:
    return str(
        client.post("/api/v1/projects", json={"name": "资料测试", "marketplace": "US"}).json()["id"]
    )


def test_asin_materials_survive_restart_and_respect_project(db: str, engine: Engine, tmp_path: Path) -> None:
    with TestClient(create_app(db, storage_root=tmp_path)) as client:
        project_id = create_project(client)
        run = client.post(
            f"/api/v1/projects/{project_id}/pipeline-runs",
            json={"marketplace": "US", "asin": " b0abcdef12 ", "description": "自有商品"},
            headers={"Idempotency-Key": "asin"},
        ).json()
        url = f"/api/v1/projects/{project_id}/pipeline-runs/{run['id']}/product-materials"
        result = client.get(url)
        assert result.status_code == 200
        body = result.json()
        assert body["schema_version"] == 1
        assert body["asin"] == "B0ABCDEF12"
        assert body["asin_resolution"] == "awaiting_provider"
        assert body["supplied_product_name"] is None
        assert body["supplied_description"] == "自有商品"
        assert body["images"] == []
        assert body["model_status"] == "not_started"
        assert body["warnings"]
        assert (
            client.get(url.removesuffix("/product-materials")).json()["status"] == "input_received"
        )
        other = create_project(client)
        denied = client.get(url.replace(project_id, other))
        assert denied.status_code == 404
        assert denied.json()["error"]["code"] == "NOT_FOUND"
    with TestClient(create_app(db, storage_root=tmp_path)) as client:
        assert client.get(url).json() == body


def test_remote_image_stays_unverified(db: str, engine: Engine, tmp_path: Path) -> None:
    with TestClient(create_app(db, storage_root=tmp_path)) as client:
        project_id = create_project(client)
        run = client.post(
            f"/api/v1/projects/{project_id}/pipeline-runs",
            json={"marketplace": "US", "image_urls": ["https://example.com/product.png"]},
            headers={"Idempotency-Key": "remote"},
        ).json()
        result = client.get(
            f"/api/v1/projects/{project_id}/pipeline-runs/{run['id']}/product-materials"
        )
        assert result.status_code == 200
        body = result.json()
        assert body["asin_resolution"] == "not_requested"
        image = body["images"][0]
        assert image["source"] == "remote_url"
        assert image["verification"] == "unverified"
        assert image["url"] == "https://example.com/product.png"
        assert image["asset"] is None
        assert image["role"] == "unspecified"
        assert body["warnings"]


def test_uploaded_and_combined_materials_report_missing_file(db: str, engine: Engine, tmp_path: Path) -> None:
    with TestClient(create_app(db, storage_root=tmp_path)) as client:
        project_id = create_project(client)
        buffer = BytesIO()
        Image.new("RGB", (20, 15), "white").save(buffer, format="PNG")
        asset = client.post(
            f"/api/v1/projects/{project_id}/images",
            files={"file": ("photo.png", buffer.getvalue(), "image/png")},
            headers={"Idempotency-Key": "upload"},
        ).json()
        url = ""
        # 图片单独输入以及 ASIN + 图片，都不能自动把任意图片认定为商品主图。
        for index, extra in enumerate(({}, {"asin": "B0ABCDEF12", "product_name": "  哑铃架  "})):
            run = client.post(
                f"/api/v1/projects/{project_id}/pipeline-runs",
                json={"marketplace": "US", "image_ids": [asset["id"]], **extra},
                headers={"Idempotency-Key": f"run-{index}"},
            ).json()
            url = f"/api/v1/projects/{project_id}/pipeline-runs/{run['id']}/product-materials"
            result = client.get(url)
            assert result.status_code == 200
            body = result.json()
            image = body["images"][0]
            assert image["source"] == "uploaded"
            assert image["verification"] == "verified_at_upload"
            assert image["role"] == "unspecified"
            assert image["asset"] == asset
            assert image["url"] == asset["content_url"]
            if extra:
                assert body["supplied_product_name"] == "哑铃架"
                assert body["asin_resolution"] == "awaiting_provider"
        (tmp_path / "uploads" / f"{asset['id']}{asset['extension']}").unlink()
        missing = client.get(url)
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "IMAGE_FILE_MISSING"
