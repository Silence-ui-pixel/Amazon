"""编排接续已有进度：从第 7 步完成状态跑到发布，不重复网络采集。"""

from pathlib import Path

from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import Engine
from test_library import ready_library

from app.domain.workflow import WorkflowView
from app.main import create_app


def test_seeded_run_reaches_publication(db: str, engine: Engine, tmp_path: Path) -> None:
    with ready_library(db, engine, tmp_path) as (_, base, database_url, _info):
        pass
    with TestClient(
        create_app(
            database_url,
            storage_root=tmp_path,
            workflow_checkpointer=InMemorySaver(),
            workflow_inline=True,
        )
    ) as api:
        started = api.post(base + "/workflow", json={"options": {"auto_decide": False}})
        assert started.status_code == 202, started.text
        view = WorkflowView.model_validate(started.json())
        assert view.status == "waiting_human"
        assert view.waiting_for is not None
        assert view.waiting_for.gate == "week"

        def resume(gate: str, revision: int, payload: dict[str, str]) -> WorkflowView:
            response = api.post(
                base + f"/workflow/{view.workflow_id}/resume",
                json={
                    "gate": gate,
                    "action": "continue",
                    "expected_revision": revision,
                    "payload": payload,
                },
            )
            assert response.status_code == 202, response.text
            return WorkflowView.model_validate(response.json())

        view = resume("week", view.revision, {"aba_week_end": "2026-10-17"})
        assert view.status == "waiting_human", view
        assert view.waiting_for is not None
        assert view.waiting_for.gate == "library_publish"
        statuses = {node.key: node.status for node in view.nodes}
        assert statuses["create_cleaning"] == "done"
        assert statuses["traffic_review"] == "skipped"
        assert statuses["create_library"] == "done"

        history = api.get(base + "/library").json()
        assert history, "编排应生成词库草稿"
        library = history[0]
        required = [entry["keyword"] for entry in library["entries"] if entry["issues"]]
        published = api.post(
            base + f"/library/{library['id']}/publish",
            json={
                "expected_version": library["version"],
                "publisher": "本地运营",
                "reason": "编排验收",
                "confirmed": True,
                "exception_reviews": [
                    {"keyword": keyword, "reason": "已人工核对"} for keyword in required
                ],
            },
        )
        assert published.status_code == 200, published.text

        view = resume("library_publish", view.revision, {})
        assert view.status == "completed", view
        assert view.waiting_for is None
        assert view.current_node is None
