from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import Engine
from test_research_records import add_run

from app.main import create_app
from app.workflows.store import WorkflowStore


def test_research_recycle_restore_and_restart(db: str, engine: Engine, tmp_path: Path) -> None:
    with TestClient(create_app(db, storage_root=tmp_path, auto_execute=False)) as client:
        run = add_run(client, "可恢复研究")
        url = f"/api/v1/projects/{run['project_id']}/research-records/{run['id']}"
        assert client.delete(url).status_code == 200
        assert client.delete(url).status_code == 200
        assert client.get("/api/v1/research-records").json()["total"] == 0
        assert (
            client.get("/api/v1/research-records?deleted=true").json()["items"][0]["id"]
            == run["id"]
        )
        assert (
            client.get(
                f"/api/v1/projects/{run['project_id']}/pipeline-runs/{run['id']}"
            ).status_code
            == 200
        )
    with TestClient(create_app(db, storage_root=tmp_path, auto_execute=False)) as client:
        assert client.get("/api/v1/research-records").json()["total"] == 0
        assert client.post(url + "/restore").status_code == 200
        assert client.get("/api/v1/research-records").json()["total"] == 1


def test_running_research_cannot_be_deleted(db: str, engine: Engine, tmp_path: Path) -> None:
    with TestClient(create_app(db, storage_root=tmp_path, auto_execute=False)) as client:
        run = add_run(client, "执行中")
        store = WorkflowStore(engine)
        store.create(
            workflow_id="active",
            run_id=run["id"],
            project_id=run["project_id"],
            state="{}",
            view="{}",
            created_at="2026-10-28T00:00:00Z",
        )
        url = f"/api/v1/projects/{run['project_id']}/research-records/{run['id']}"
        assert client.delete(url).status_code == 409
        assert client.get("/api/v1/research-records").json()["total"] == 1
