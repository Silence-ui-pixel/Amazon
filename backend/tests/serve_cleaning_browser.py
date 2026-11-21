"""第六步隔离浏览器环境，所有外部来源使用测试样本。"""

import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import httpx
import uvicorn
from conftest import CHECKPOINT_TABLES, _ensure_test_database
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from test_aba import auth_file, row
from test_aba_expansion import prepare, wait

from app.adapters.aba import AbaClient
from app.db import config
from app.db.models import metadata
from app.main import create_app
from app.services.qwen import QwenClient, QwenConfig
from app.workflows.checkpointer import postgres_checkpointer

if __name__ == "__main__":
    with tempfile.TemporaryDirectory(
        prefix="cleaning-browser-", dir=Path(__file__).resolve().parents[1] / "data"
    ) as folder:
        path = Path(folder)
        source = make_url(config.database_url())
        database = source.set(database=f"{source.database}_browser_traffic").render_as_string(
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
        base = prepare(database, engine, path)
        terms = ["portable home gym", "adjustable gym", "foldable outdoor gym", "=1+1"] + [
            "gym " + chr(97 + i) + "style" for i in range(23)
        ]
        aba = AbaClient(
            auth_file(path),
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    json={
                        "reportsV2": [
                            {
                                "totalItems": len(terms),
                                "rows": [row(term, i + 200000) for i, term in enumerate(terms)],
                            }
                        ]
                    },
                )
            ),
        )
        with TestClient(create_app(database, aba_client=aba)) as api:
            for direction in ("forward", "reverse"):
                r = api.post(
                    base + "/aba-expansion",
                    json={"direction": direction, "week_end": "2026-10-17", "max_rows": 100},
                ).json()
                assert wait(api, base + "/aba-expansion", r["id"])["status"] == "succeeded"
            for direction, query in [
                ("forward", "cooling blanket"),
                ("reverse", "B012345678"),
                ("reverse", "B087654321"),
            ]:
                r = api.post(
                    base + "/third-party/preview",
                    files={"file": ("seller.csv", ("keyword\n" + "\n".join(terms)).encode())},
                    data={
                        "metadata": json.dumps(
                            {
                                "direction": direction,
                                "query": query,
                                "marketplace": "US",
                                "period_kind": "last_30_days",
                                "collected_at": datetime.now(UTC).isoformat(),
                                "source_total": len(terms),
                                "export_complete": True,
                            }
                        )
                    },
                ).json()
                assert (
                    api.post(
                        base + f"/third-party/{r['id']}/confirm",
                        json={"mapping": {"keyword": "keyword"}, "share_unit": "fraction"},
                    ).status_code
                    == 200
                )
        def classify(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            prompt = body["messages"][0]["content"][0]["text"]
            data = json.loads(prompt.split("数据：", 1)[1])
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "content": json.dumps(
                                    {
                                        "roots": [
                                            {
                                                "term": item["term"],
                                                "category": "scene"
                                                if item["term"] in {"home", "gym", "outdoor"}
                                                else "preference",
                                                "evidence": "结合隔离样本商品属性自动判断",
                                            }
                                            for item in data["frequencies"]
                                        ],
                                        "exclusions": [],
                                        "reason": "隔离样本自动分类完成",
                                    },
                                    ensure_ascii=False,
                                )
                            },
                        }
                    ]
                },
            )

        app = create_app(
            database,
            model_client=QwenClient(
                QwenConfig(api_key="test"), transport=httpx.MockTransport(classify)
            ),
        )
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
