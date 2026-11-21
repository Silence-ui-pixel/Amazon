"""第二步真实接口测试；破坏排名来源、筛选门槛或计费幂等均应失败。"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from test_aba import auth_file, row, seed_run

from app.adapters.aba import AbaClient
from app.db.repository import Repository
from app.main import create_app
from app.services.qwen import QwenClient, QwenConfig

PRODUCT = {
    "product_name": "凉感毯",
    "product_type": "毯子",
    "features": ["凉感"],
    "uncertainties": [],
}
TERMS = [
    ("blanket", 10),
    ("cooling blanket", 30),
    ("electric blanket", 20),
    ("Acme cooling blanket", 40),
    ("Other cooling blanket", 50),
    ("blanketcase", 1),
]


def setup_case(
    db: str, engine: Engine, tmp_path: Path, mode: str = "ok"
) -> tuple[TestClient, str, str, list[str]]:
    calls: list[str] = []

    def aba(request: httpx.Request) -> httpx.Response:
        page = json.loads(request.content)["reportOperations"][0]["pageNumber"]
        return httpx.Response(
            200,
            json={
                "reportsV2": [
                    {
                        "totalItems": len(TERMS),
                        "rows": [row(t, r) for t, r in TERMS[(page - 1) * 100 : page * 100]],
                    }
                ]
            },
        )

    def model(request: httpx.Request) -> httpx.Response:
        calls.append("model")
        if mode == "timeout":
            raise httpx.ReadTimeout("private upstream detail", request=request)
        prompt = json.loads(request.content)["messages"][0]["content"][0]["text"]
        data = json.loads(prompt.split("数据：", 1)[1])
        if "相关性粗筛" in prompt:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "content": json.dumps(
                                    {"candidate_ids": [c["id"] for c in data["candidates"]]}
                                )
                            },
                        }
                    ]
                },
            )
        items: list[dict[str, Any]] = []
        for candidate in data["candidates"]:
            term = candidate["keyword"]
            items.append(
                {
                    "candidate_id": candidate["id"],
                    "translation": "毯子相关词",
                    "relevance": "low" if term == "electric blanket" else "high",
                    "intent": "exact",
                    "category": "own_brand"
                    if term.startswith("Acme")
                    else "competitor_brand"
                    if term.startswith("Other")
                    else "generic",
                    "reason": "依据商品凉感功能判断",
                }
            )
        if mode == "generic_brand":
            for item in items:
                item["category"] = "generic"
        if mode != "missing_role":
            for item in items:
                item["keyword_role"] = "core"
        if mode == "unknown":
            items[0]["candidate_id"] = "00000000-0000-0000-0000-000000000000"
        if mode == "duplicate":
            items.append(items[0])
        if mode == "missing":
            items.pop()
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"finish_reason": "stop", "message": {"content": json.dumps({"items": items})}}
                ]
            },
        )

    client = TestClient(
        create_app(
            db,
            storage_root=tmp_path,
            aba_client=AbaClient(auth_file(tmp_path), transport=httpx.MockTransport(aba)),
            model_client=QwenClient(
                QwenConfig(api_key="test-secret"), transport=httpx.MockTransport(model)
            ),
        )
    )
    client.__enter__()
    project, run, _ = seed_run(client, engine, tmp_path)
    with Repository(engine, tmp_path).connect() as conn:
        candidates = json.loads(
            conn.execute("SELECT document FROM root_candidates WHERE run_id=?", (run,)).fetchone()[
                0
            ]
        )
        candidates["candidates"][0]["text"] = "blanket"
        conn.execute(
            "UPDATE root_candidates SET document=? WHERE run_id=?", (json.dumps(candidates), run)
        )
        understanding: dict[str, Any] = {
            "project_id": project,
            "run_id": run,
            "provider": "bailian",
            "model": "test",
            "created_at": datetime.now(UTC).isoformat(),
            "product": PRODUCT,
            "warnings": [],
        }
        conn.execute(
            "INSERT INTO product_understandings VALUES (?, 'succeeded', ?)",
            (run, json.dumps(understanding)),
        )
    base = f"/api/v1/projects/{project}/pipeline-runs/{run}"
    report = client.post(base + "/aba-reports", json={"week_end": "2026-10-17"})
    assert report.status_code == 200, report.text
    return client, base, report.json()["id"], calls


def test_candidates_analysis_selection_and_restart(db: str, engine: Engine, tmp_path: Path) -> None:
    client, base, report, calls = setup_case(db, engine, tmp_path)
    try:
        url = base + f"/aba-reports/{report}"
        response = client.get(url + "/core-candidates")
        assert response.status_code == 200, response.text
        candidates = response.json()
        assert [r["keyword"] for r in candidates["items"]] == [
            "blanket",
            "electric blanket",
            "cooling blanket",
            "Acme cooling blanket",
            "Other cooling blanket",
        ]
        assert candidates["items"][0]["root_reference"] is True
        request = {"own_brands": ["Acme"]}
        response = client.post(url + "/core-analyses", json=request)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["status"] == "succeeded"
        assert result["total_candidates"] == 5 and result["remaining_candidates"] == 0
        assert [
            (x["keyword"], x["search_frequency_rank"]) for x in result["items"] if x["recommended"]
        ] == [("cooling blanket", 30)]
        assert [x["keyword"] for x in result["items"] if x["category"] == "own_brand"] == [
            "Acme cooling blanket"
        ]
        assert [x["keyword"] for x in result["items"] if x["category"] == "competitor_brand"] == [
            "Other cooling blanket"
        ]
        assert client.post(url + "/core-analyses", json=request).json()["id"] == result["id"]
        assert calls == ["model"]
        core = next(x for x in result["items"] if x["recommended"])
        saved = client.put(
            base + "/core-selection",
            json={
                "analysis_id": result["id"],
                "candidate_ids": [core["id"]],
                "expected_version": 0,
            },
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["version"] == 1
        assert client.get(base + "/core-selection").json() == saved.json()
        assert (
            client.put(
                base + "/core-selection",
                json={
                    "analysis_id": result["id"],
                    "candidate_ids": [core["id"]],
                    "expected_version": 0,
                },
            ).json()
            == saved.json()
        )
        pure = candidates["items"][0]["id"]
        assert (
            client.put(
                base + "/core-selection",
                json={"analysis_id": result["id"], "candidate_ids": [pure], "expected_version": 1},
            ).status_code
            == 422
        )
        brand = next(x for x in result["items"] if x["category"] == "own_brand")
        brand_rejected = client.put(
            base + "/core-selection",
            json={
                "analysis_id": result["id"],
                "candidate_ids": [brand["id"]],
                "expected_version": 1,
            },
        )
        assert brand_rejected.status_code == 422
        assert "无需确认" in brand_rejected.text
        assert client.get(base + "/root-selection").status_code == 200
        other = client.post("/api/v1/projects", json={"name": "other", "marketplace": "US"}).json()[
            "id"
        ]
        project = base.split("/")[4]
        assert (
            client.get(
                (base + f"/core-analyses/{result['id']}").replace(project, other)
            ).status_code
            == 404
        )
    finally:
        client.__exit__(None, None, None)
    with TestClient(create_app(db, storage_root=tmp_path)) as reopened:
        assert reopened.get(base + "/core-selection").json() == saved.json()
        assert reopened.get(base + f"/core-analyses/{result['id']}").json() == result


@pytest.mark.parametrize(
    "mode,code",
    [
        ("unknown", "MODEL_OUTPUT_INVALID"),
        ("duplicate", "MODEL_OUTPUT_INVALID"),
        ("missing", "MODEL_OUTPUT_INVALID"),
        ("timeout", "MODEL_OUTCOME_UNKNOWN"),
    ],
)
def test_invalid_or_uncertain_model_is_not_silently_retried(
    db: str, engine: Engine, tmp_path: Path, mode: str, code: str
) -> None:
    client, base, report, calls = setup_case(db, engine, tmp_path, mode)
    try:
        url = base + f"/aba-reports/{report}/core-analyses"
        response = client.post(url, json={})
        assert response.status_code >= 400
        assert response.json()["error"]["code"] == code
        again = client.post(url, json={})
        assert again.status_code == 409
        assert calls == ["model"]
        assert "private upstream detail" not in response.text
    finally:
        client.__exit__(None, None, None)


def test_partial_analysis_unknown_brand_and_stale_inputs(db: str, engine: Engine, tmp_path: Path) -> None:
    client, base, report, calls = setup_case(db, engine, tmp_path)
    try:
        url = base + f"/aba-reports/{report}/core-analyses"
        result = client.post(url, json={"offset": 2, "limit": 3}).json()
        assert result["remaining_candidates"] == 2
        brand = next(x for x in result["items"] if x["keyword"].startswith("Acme"))
        assert brand["category"] == "brand_unknown" and not brand["recommended"]
        core = next(x for x in result["items"] if x["recommended"])
        with Repository(engine, tmp_path).connect() as conn:
            product = json.loads(
                conn.execute("SELECT document FROM product_understandings").fetchone()[0]
            )
            product["product"]["features"] = ["电热"]
            conn.execute("UPDATE product_understandings SET document=?", (json.dumps(product),))
        old = client.get(base + f"/core-analyses/{result['id']}")
        assert old.json()["stale"] is True
        assert (
            client.put(
                base + "/core-selection",
                json={
                    "analysis_id": result["id"],
                    "candidate_ids": [core["id"]],
                    "expected_version": 0,
                },
            ).status_code
            == 409
        )
        assert client.post(url, json={}).status_code == 409
        assert calls == ["model"]
    finally:
        client.__exit__(None, None, None)


def test_explicit_own_brand_cannot_be_recommended_as_generic(db: str, engine: Engine, tmp_path: Path) -> None:
    client, base, report, _ = setup_case(db, engine, tmp_path, "generic_brand")
    try:
        result = client.post(
            base + f"/aba-reports/{report}/core-analyses", json={"own_brands": ["Acme"]}
        ).json()
        brand = next(x for x in result["items"] if x["keyword"].startswith("Acme"))
        assert brand["category"] == "own_brand"
        assert brand["recommended"] is False
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize(
    "change,code", [("rank", "CORE_RANK_CONFLICT"), ("asin", "CORE_FORWARD_REPORT_REQUIRED")]
)
def test_source_preconditions_fail_before_model(db: str, engine: Engine, tmp_path: Path, change: str, code: str) -> None:
    client, base, report, calls = setup_case(db, engine, tmp_path)
    try:
        with Repository(engine, tmp_path).connect() as conn:
            document = json.loads(
                conn.execute("SELECT document FROM aba_reports WHERE id=?", (report,)).fetchone()[0]
            )
            if change == "rank":
                duplicate = dict(document["reports"][0]["rows"][1])
                duplicate["search_frequency_rank"] = 999
                document["reports"][0]["rows"].append(duplicate)
            else:
                document["query_mode"] = "asin"
            conn.execute(
                "UPDATE aba_reports SET document=? WHERE id=?", (json.dumps(document), report)
            )
        response = client.post(base + f"/aba-reports/{report}/core-analyses", json={})
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == code
        assert calls == []
    finally:
        client.__exit__(None, None, None)


def test_legacy_six_item_selection_without_rule_metadata_remains_readable(db: str, engine: Engine, tmp_path: Path) -> None:
    # Rejecting old JSON or routing it to new analysis storage breaks persisted workspaces.
    from copy import deepcopy
    from uuid import uuid4

    client, base, report, _ = setup_case(db, engine, tmp_path)
    try:
        analysis = client.post(base + f"/aba-reports/{report}/core-analyses", json={}).json()
        recommended = next(item for item in analysis["items"] if item["recommended"])
        saved = client.put(
            base + "/core-selection",
            json={
                "analysis_id": analysis["id"],
                "candidate_ids": [recommended["id"]],
                "expected_version": 0,
            },
        ).json()
        saved.pop("rule_version", None)
        saved.pop("source_analysis_id", None)
        saved["items"] = []
        for index in range(6):
            item = deepcopy(recommended)
            item.update(id=str(uuid4()), keyword=f"legacy core {index}")
            saved["items"].append(item)
        with Repository(engine, tmp_path).connect() as conn:
            conn.execute("UPDATE core_selections SET document=?", (json.dumps(saved),))
        result = client.get(base + "/core-selection")
        assert result.status_code == 200, result.text
        assert [item["keyword"] for item in result.json()["items"]] == [
            f"legacy core {index}" for index in range(6)
        ]
        assert result.json()["rule_version"] == "core-terms-v1"
        assert result.json()["source_analysis_id"] is None
    finally:
        client.__exit__(None, None, None)
