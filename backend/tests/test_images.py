"""验证真实图片内容、保存读取、项目归属与输入任务关联。"""

from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import Engine

from app.main import create_app


def image_bytes(format_name: str = "PNG") -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (16, 12), "white").save(buffer, format=format_name)
    return buffer.getvalue()


def project(client: TestClient) -> str:
    return str(
        client.post("/api/v1/projects", json={"name": "图片测试", "marketplace": "US"}).json()["id"]
    )


@pytest.mark.parametrize(
    "format_name,content_type",
    [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")],
)
def test_upload_preview_and_restart(db: str, engine: Engine, tmp_path: Path, format_name: str, content_type: str) -> None:
    content = image_bytes(format_name)
    with TestClient(create_app(db, storage_root=tmp_path)) as client:
        project_id = project(client)
        result = client.post(
            f"/api/v1/projects/{project_id}/images",
            files={"file": ("../product.fake", content, "image/png")},
            headers={"Idempotency-Key": "image-first"},
        )
        assert result.status_code == 201
        asset = result.json()
        assert asset["content_type"] == content_type
        assert (asset["width"], asset["height"]) == (16, 12)
        assert asset["size_bytes"] == len(content)
        assert result.headers["location"] == asset["content_url"]
    with TestClient(create_app(db, storage_root=tmp_path)) as client:
        preview = client.get(asset["content_url"])
        assert preview.status_code == 200
        assert preview.content == content
        assert preview.headers["content-type"] == content_type
        assert preview.headers["x-content-type-options"] == "nosniff"
        assert ".." not in asset["original_name"]
        # 只提供已上传图片也能保存商品输入，无需虚构商品名称。
        task = client.post(
            f"/api/v1/projects/{project_id}/pipeline-runs",
            json={"marketplace": "US", "image_ids": [asset["id"]]},
            headers={"Idempotency-Key": "image-run"},
        )
        assert task.status_code == 201
        assert task.json()["input"]["image_ids"] == [asset["id"]]
        other = project(client)
        assert (
            client.get(f"/api/v1/projects/{other}/images/{asset['id']}/content").status_code == 404
        )
        assert (
            client.post(
                f"/api/v1/projects/{other}/pipeline-runs",
                json={"marketplace": "US", "image_ids": [asset["id"]]},
                headers={"Idempotency-Key": "foreign-image"},
            ).status_code
            == 404
        )


def test_rejects_invalid_and_oversized_files(db: str, engine: Engine, tmp_path: Path) -> None:
    with TestClient(create_app(db, storage_root=tmp_path)) as client:
        url = f"/api/v1/projects/{project(client)}/images"
        for data in (b"", b"not an image", image_bytes()[:30]):
            result = client.post(
                url,
                files={"file": ("fake.png", data, "image/png")},
                headers={"Idempotency-Key": "bad"},
            )
            assert result.status_code == 422
            assert result.json()["error"]["code"] == "INVALID_IMAGE"
        unsupported = client.post(
            url,
            files={"file": ("photo.gif", image_bytes("GIF"), "image/gif")},
            headers={"Idempotency-Key": "gif"},
        )
        assert unsupported.status_code == 422
        large = client.post(
            url,
            files={"file": ("large.png", b"x" * (10 * 1024**2 + 1))},
            headers={"Idempotency-Key": "large"},
        )
        assert large.status_code == 413
        assert list((tmp_path / "uploads").glob("*")) == []


def test_upload_idempotency_and_total_image_limit(db: str, engine: Engine, tmp_path: Path) -> None:
    with TestClient(create_app(db, storage_root=tmp_path)) as client:
        project_id = project(client)
        url = f"/api/v1/projects/{project_id}/images"
        files = {"file": ("photo.png", image_bytes(), "image/png")}
        headers = {"Idempotency-Key": "retry"}
        first = client.post(url, files=files, headers=headers)
        second = client.post(url, files=files, headers=headers)
        assert first.status_code == second.status_code == 201
        assert first.json() == second.json()
        assert len(list((tmp_path / "uploads").glob("*"))) == 1
        changed = client.post(
            url,
            files={"file": ("different.png", image_bytes("JPEG"))},
            headers={"Idempotency-Key": "retry"},
        )
        assert changed.status_code == 409
        assert len(list((tmp_path / "uploads").glob("*"))) == 1
        run_url = f"/api/v1/projects/{project_id}/pipeline-runs"
        invalid = client.post(
            run_url,
            json={
                "marketplace": "US",
                "image_ids": [first.json()["id"]],
                "image_urls": ["https://example.com/a.jpg"] * 5,
            },
            headers={"Idempotency-Key": "too-many"},
        )
        assert invalid.status_code == 422
        missing = client.post(
            run_url,
            json={"marketplace": "US", "image_ids": ["00000000-0000-0000-0000-000000000000"]},
            headers={"Idempotency-Key": "missing"},
        )
        assert missing.status_code == 404
