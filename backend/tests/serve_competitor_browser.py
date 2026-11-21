"""第三步隔离浏览器验收，外部模型与 ABA 仅用测试传输。"""

import tempfile
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from test_core_terms import setup_case

if __name__ == "__main__":
    with tempfile.TemporaryDirectory(
        prefix="competitor-browser-", dir=Path(__file__).resolve().parents[1] / "data"
    ) as folder:
        client, base, report, calls = setup_case(Path(folder))
        analysis = client.post(base + f"/aba-reports/{report}/core-analyses", json={}).json()
        response = client.put(
            base + "/core-selection",
            json={
                "analysis_id": analysis["id"],
                "candidate_ids": [x["id"] for x in analysis["items"] if x["recommended"]],
                "expected_version": 0,
            },
        )
        assert response.status_code == 200, response.text
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
            uvicorn.run(client.app, host="127.0.0.1", port=8014, log_level="warning")
        finally:
            client.__exit__(None, None, None)
