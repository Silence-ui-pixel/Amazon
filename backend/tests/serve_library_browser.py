"""第八步浏览器验收，隔离数据库与外部服务测试替身。"""

import tempfile
from pathlib import Path

import uvicorn
from conftest import CHECKPOINT_TABLES, _ensure_test_database
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from test_library import ready_library

from app.db import config
from app.db.models import metadata
from app.main import create_app
from app.workflows.checkpointer import postgres_checkpointer

if __name__ == "__main__":
    with tempfile.TemporaryDirectory(
        prefix="library-browser-", dir=Path(__file__).resolve().parents[1] / "data"
    ) as folder:
        path = Path(folder)
        source = make_url(config.database_url())
        database = source.set(database=f"{source.database}_browser_library").render_as_string(
            hide_password=False
        )
        _ensure_test_database(database)
        engine = create_engine(database, pool_pre_ping=True)
        metadata.create_all(bind=engine)
        with postgres_checkpointer(config.psycopg_url(database)):
            pass
        with engine.begin() as conn:
            conn.execute(text(f"TRUNCATE {', '.join(metadata.tables)} RESTART IDENTITY CASCADE"))
            conn.execute(
                text(f"TRUNCATE {', '.join(CHECKPOINT_TABLES)} RESTART IDENTITY CASCADE")
            )
        with ready_library(database, engine, path) as (_, base, _, _request):
            pass
        app = create_app(database, storage_root=path)
        project, run = base.split("/")[4], base.split("/")[6]

        @app.get("/__test__/seed")
        def seed() -> dict[str, object]:
            return {
                "run": {"project_id": project, "id": run, "status": "input_received"},
                "marketplace": "US",
                "asin": "B0G3GRFJYB",
                "root": "",
                "product": "",
                "sources": ["asin"],
                "reference": "",
                "verificationId": "",
            }

        try:
            uvicorn.run(app, host="127.0.0.1", port=8014, log_level="warning")
        finally:
            engine.dispose()
