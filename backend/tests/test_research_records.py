"""历史入口只读列举所有研究，不合并相同 ASIN，不触发外部工作。"""

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.db.repository import Repository
from app.main import create_app
from app.workflows.store import WorkflowStore


def add_run(client: TestClient, key: str, asin: str | None = "B0CGV33WDY") -> dict[str, Any]:
    project = client.post("/api/v1/projects", json={"name": key, "marketplace": "US"}).json()
    return client.post(
        f"/api/v1/projects/{project['id']}/pipeline-runs",
        headers={"Idempotency-Key": "history-test"},
        json={
            "marketplace": "US",
            "asin": asin,
            "product_name": key,
            "image_urls": [] if asin else ["https://example.com/a.jpg"],
        },
    ).json()


def test_history_keeps_each_run_searches_and_paginates(db: str, engine: Engine, tmp_path: Path) -> None:
    with TestClient(create_app(db, storage_root=tmp_path, auto_execute=False)) as client:
        first = add_run(client, "旧研究")
        second = add_run(client, "新研究")
        image = add_run(client, "图片商品", None)
        response = client.get("/api/v1/research-records", params={"q": "b0cgv33wdy", "limit": 1})
        assert response.status_code == 200
        page = response.json()
        assert page["total"] == 2
        assert [x["id"] for x in page["items"]] == [second["id"]]
        assert page["items"][0]["status"] == "input_received"
        assert page["items"][0]["library"] is None
        older = client.get("/api/v1/research-records?q=B0CGV33WDY&limit=1&offset=1").json()
        assert older["items"][0]["id"] == first["id"]
        assert (
            client.get("/api/v1/research-records?q=图片商品").json()["items"][0]["id"]
            == image["id"]
        )
        assert client.get("/api/v1/research-records?q=%").json()["total"] == 0
        assert client.get("/api/v1/research-records?limit=0").status_code == 422


def test_history_uses_latest_workflow_and_summarizes_library_without_raw_data(
    db: str, engine: Engine, tmp_path: Path,
) -> None:
    with TestClient(create_app(db, storage_root=tmp_path, auto_execute=False)) as client:
        run = add_run(client, "同一商品")
        store = WorkflowStore(engine)
        for workflow_id, status, date in [
            ("old", "failed", "2026-10-26T10:00:00Z"),
            ("new", "completed", "2026-10-27T10:00:00Z"),
        ]:
            view: dict[str, Any] = {
                "status": status,
                "current_node": None,
                "nodes": [],
                "error": "",
            }
            store.create(
                workflow_id=workflow_id,
                run_id=run["id"],
                project_id=run["project_id"],
                state=json.dumps({"aba_week_end": "2026-10-24"}),
                view=json.dumps(view),
                created_at=date,
            )
            with Repository(engine, tmp_path).connect() as conn:
                conn.execute(
                    "UPDATE workflow_runs SET status=? WHERE workflow_id=?", (status, workflow_id)
                )
        library = {
            "id": "saved-library",
            "created_at": "2026-10-27T11:00:00Z",
            "publication_version": 2,
            "stale": False,
            "entries": [{"evidence": {"metrics": {"rank": rank}}} for rank in [100, None, 0]],
            "snapshots": {"private": "DO_NOT_EXPOSE"},
        }
        with Repository(engine, tmp_path).connect() as conn:
            conn.execute(
                "INSERT INTO keyword_library_drafts VALUES(?,?,?,?,?,?,?)",
                (
                    "saved-library",
                    run["id"],
                    run["project_id"],
                    "traffic",
                    1,
                    json.dumps(library),
                    "",
                ),
            )
        response = client.get("/api/v1/research-records?status=completed")
        assert response.status_code == 200
        row = response.json()["items"][0]
        assert row["workflow_id"] == "new"
        assert row["report_week"] == "2026-10-24"
        assert row["library"]["total"] == 3
        assert row["library"]["ranked"] == 1
        assert row["library"]["publication_version"] == 2
        assert "DO_NOT_EXPOSE" not in response.text
        assert client.get("/api/v1/research-records?status=failed").json()["total"] == 0
        assert client.get("/api/v1/research-records?status=madeup").status_code == 422
