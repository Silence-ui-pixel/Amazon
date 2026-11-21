"""隔离浏览器联调后端：真实接口与 SQLite，仅外部 ABA/模型用合成响应。"""

import tempfile
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from test_core_terms import setup_case

if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="core-browser-") as folder:
        client, base, report, calls = setup_case(Path(folder))
        assert isinstance(client.app, FastAPI)
        project, run = base.split("/")[4], base.split("/")[6]

        @client.app.get("/__test__/seed")
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

        @client.app.get("/__test__/calls")
        def counts() -> dict[str, int]:
            return {"model": len(calls)}

        try:
            uvicorn.run(client.app, host="127.0.0.1", port=8012, log_level="warning")
        finally:
            client.__exit__(None, None, None)
