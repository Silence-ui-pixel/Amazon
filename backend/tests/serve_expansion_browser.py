"""第四步隔离浏览器验收；本地真实 API，外部 ABA 使用测试传输。"""

import tempfile
import time
from pathlib import Path

import httpx
import uvicorn
from test_aba import auth_file, row
from test_aba_expansion import prepare

from app.adapters.aba import AbaClient
from app.main import create_app

if __name__ == "__main__":
    with tempfile.TemporaryDirectory(
        prefix="expansion-browser-", dir=Path(__file__).resolve().parents[1] / "data"
    ) as folder:
        path = Path(folder)
        base, database = prepare(path)
        calls: list[str] = []

        def handle(request: httpx.Request) -> httpx.Response:
            import json

            body = json.loads(request.content)
            query = next(
                x for x in body["filterSelections"] if x["id"] in ("asins", "search-term-freeform")
            )
            calls.append(query["value"])
            time.sleep(0.2)
            rows = [row(f"cooling blanket {i}", 100 + i) for i in range(23)]
            rows.append(row("only " + query["id"], 200))
            return httpx.Response(
                200, json={"reportsV2": [{"totalItems": len(rows), "rows": rows}]}
            )

        app = create_app(
            database,
            aba_client=AbaClient(auth_file(path), transport=httpx.MockTransport(handle)),
        )
        project, run = base.split("/")[4], base.split("/")[6]

        @app.get("/__test__/seed")
        def seed() -> dict[str, object]:
            return {
                "run": {"project_id": project, "id": run, "status": "input_received"},
                "reportWeek": "2026-10-17",
                "marketplace": "US",
                "asin": "B0G3GRFJYB",
                "root": "",
                "product": "",
                "sources": ["asin"],
                "reference": "",
                "verificationId": "",
            }

        @app.get("/__test__/calls")
        def counts() -> dict[str, object]:
            return {"aba": len(calls), "queries": calls}

        uvicorn.run(app, host="127.0.0.1", port=8014, log_level="warning")
