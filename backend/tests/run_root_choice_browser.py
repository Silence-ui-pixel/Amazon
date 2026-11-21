"""人工选词后验证的真实 API 浏览器验收，外部模型与搜索使用隔离替身。"""

import json
import subprocess
import tempfile
import time
from pathlib import Path
from threading import Thread

import httpx
import uvicorn
from fastapi.testclient import TestClient
from test_roots import CANDIDATES, PRODUCT
from test_verification_jobs import FakeSearch, page

from app.main import create_app
from app.services.qwen import QwenClient, QwenConfig

if __name__ == "__main__":
    with tempfile.TemporaryDirectory(
        prefix="root-choice-", dir=Path(__file__).resolve().parents[1] / "data"
    ) as folder:
        calls: list[str] = []

        def handle(_: httpx.Request) -> httpx.Response:
            calls.append("model")
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "content": json.dumps(PRODUCT if len(calls) == 1 else CANDIDATES)
                            },
                        }
                    ]
                },
            )

        search = FakeSearch({("mug", 1): page(2), ("coffee mug", 1): page(3)})
        app = create_app(
            Path(folder) / "db.sqlite3",
            model_client=QwenClient(
                QwenConfig(api_key="test"), transport=httpx.MockTransport(handle)
            ),
            search_provider=search,
        )
        with TestClient(app) as api:
            project = api.post(
                "/api/v1/projects", json={"name": "人工选词验收", "marketplace": "US"}
            ).json()["id"]
            run = api.post(
                f"/api/v1/projects/{project}/pipeline-runs",
                json={"marketplace": "US", "asin": "B0ABCDEF12", "description": "红色杯子"},
                headers={"Idempotency-Key": "root-choice"},
            ).json()
            base = f"/api/v1/projects/{project}/pipeline-runs/{run['id']}"
            response = api.post(base + "/product-understanding")
            assert response.status_code == 200, response.text

            @app.get("/__test__/seed")
            def seed() -> dict[str, object]:
                return {
                    "run": run,
                    "marketplace": "US",
                    "asin": "B0ABCDEF12",
                    "root": "",
                    "product": "红色杯子",
                    "sources": ["asin"],
                    "reference": "",
                    "verificationId": "",
                }

            @app.get("/__test__/calls")
            def observed() -> dict[str, object]:
                return {"search": search.calls, "model": len(calls)}

            server = uvicorn.Server(
                uvicorn.Config(
                    app, host="127.0.0.1", port=8016, log_level="warning", lifespan="off"
                )
            )
            thread = Thread(target=server.run)
            thread.start()
            try:
                for _ in range(100):
                    if server.started:
                        break
                    time.sleep(0.05)
                subprocess.run(["node", "tests/browser_root_choice_smoke.cjs"], check=True)
            finally:
                server.should_exit = True
                thread.join(timeout=10)
