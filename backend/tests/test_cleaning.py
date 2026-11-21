"""第六步词频与人工确认行为。"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from test_aba import auth_file, row
from test_aba_expansion import prepare, wait
from test_third_party import MAPPING, preview

from app.adapters.aba import AbaClient
from app.db.repository import Repository
from app.domain.cleaning import CleaningPage, CleaningReport, Review, analyze
from app.domain.errors import DomainError
from app.domain.schemas import ProductInput, ProjectCreate
from app.main import create_app
from app.services.cleaning import CleaningService
from app.services.qwen import QwenClient, QwenConfig


def test_frequency_counts_every_keyword_row_and_only_returns_single_roots(db: str, engine: Engine, ) -> None:
    rows, total = analyze(["Home home gym", "home for gym", "GYMS", "gyms", "中文"])
    words = {r.term: r for r in rows}
    assert total == 8
    assert words["home"].occurrences == 3
    assert words["home"].phrase_count == 2
    assert round(words["home"].density, 2) == 37.5
    assert "home gym" not in words
    assert words["gym"].occurrences == 2
    assert words["gyms"].occurrences == 2
    assert "for" not in words


def test_no_plural_or_spelling_merge_and_no_numeric_candidates(db: str, engine: Engine, ) -> None:
    rows, total = analyze(["blanket", "blankets", "blankt", "12 portable", "portable"])
    words = {r.term: r for r in rows}
    assert {"blanket", "blankets", "blankt"} <= words.keys()
    assert "12" not in words
    assert words["portable"].phrase_count == 2
    assert total == 5


def test_four_sources_review_restore_and_conflict(
    db: str, engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = prepare(db, engine, tmp_path)
    aba = AbaClient(
        auth_file(tmp_path),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "reportsV2": [
                        {"totalItems": 1, "rows": [row("portable cooling blankets", 200001)]}
                    ]
                },
            )
        ),
    )
    request = {
        "aba_week_end": "2026-10-17",
        "seller_start": "2026-10-01",
        "seller_end": "2026-10-31",
        "text_only_acknowledged": True,
    }
    url = base + "/cleaning"
    def classify(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        prompt = body["messages"][0]["content"][0]["text"]
        payload = json.loads(prompt.split("数据：", 1)[1])
        terms = [item["term"] for item in payload["frequencies"]]
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
                                            "term": term,
                                            "category": "preference"
                                            if term in {"blanket", "blankets"}
                                            else "selling_point",
                                            "evidence": "结合当前商品类型和已确认特征判断",
                                        }
                                        for term in terms
                                    ],
                                    "exclusions": [],
                                    "reason": "系统结合当前商品事实完成自动分类",
                                }
                            )
                        },
                    }
                ]
            },
        )

    model = QwenClient(QwenConfig(api_key="test"), transport=httpx.MockTransport(classify))
    with TestClient(create_app(db, storage_root=tmp_path, aba_client=aba, model_client=model)) as api:
        assert api.post(url, json={**request, "seller_end": "2026-09-30"}).status_code == 422
        assert api.post(url, json={**request, "seller_start": "2026-11-01"}).status_code == 422
        assert api.post(url, json=request).status_code == 409
        for direction in ("forward", "reverse"):
            report = api.post(
                base + "/aba-expansion",
                json={"direction": direction, "week_end": "2026-10-17", "max_rows": 100},
            ).json()
            assert wait(api, base + "/aba-expansion", report["id"])["status"] == "succeeded"
        for direction, query, extra in [
            ("forward", "cooling blanket", None),
            (
                "reverse",
                "B012345678,B087654321",
                {
                    "collection_method": "traffic_extension",
                    "source_queries": ["B012345678", "B087654321"],
                    "variant_scope": "all",
                },
            ),
        ]:
            imported = preview(api, base + "/third-party", query, direction, metadata_extra=extra)
            assert (
                api.post(
                    base + f"/third-party/{imported['id']}/confirm",
                    json={"mapping": MAPPING, "share_unit": "percent"},
                ).status_code
                == 200
            )
        response = api.post(url, json=request)
        assert response.status_code == 200, response.text
        report = response.json()
        assert len(report["sources"]) == 4
        assert report["phrase_count"] == 8
        assert api.post(url, json=request).json()["id"] == report["id"]
        key = report["id"]
        # 后台工作流和前端可能同时启动同一快照的自动分类。模拟另一请求在本请求
        # 写入前率先保存完整结果；当前请求应读取既有成功结果，而不是报版本冲突。
        original_review = CleaningService.review
        raced = False

        def finish_in_competing_request(
            service: CleaningService,
            project: UUID,
            run: UUID,
            analysis: UUID,
            review: Review,
        ) -> CleaningReport:
            nonlocal raced
            if not raced:
                raced = True
                original_review(service, project, run, analysis, review)
            return original_review(service, project, run, analysis, review)

        monkeypatch.setattr(CleaningService, "review", finish_in_competing_request)
        classified = api.post(url + f"/{key}/classify")
        monkeypatch.setattr(CleaningService, "review", original_review)
        assert classified.status_code == 200, classified.text
        assert classified.json()["version"] == 1
        assert {r["term"] for r in classified.json()["review"]["roots"]} == {
            "blanket",
            "blankets",
            "cooling",
            "portable",
        }
        assert all(r["evidence"] for r in classified.json()["review"]["roots"])
        assert api.get(url + f"/{key}/rows?kind=phrases&limit=1").json()["total"] == 8
        rows = api.get(url + f"/{key}/rows?kind=phrases").json()["phrases"]
        assert len([p for p in rows if p["keyword"] == "cooling blanket"]) == 4
        assert all(len(p["evidence"]) == 1 for p in rows)
        review = {
            "expected_version": 1,
            "roots": [
                {
                    "term": "portable",
                    "category": "preference",
                    "evidence": "历史兼容性测试",
                }
            ],
            "exclusions": [{"keyword": "=1+1", "reason": "非搜索词"}],
            "reason": "人工核对",
        }
        assert api.post(url + f"/{key}/classify").json()["version"] == 1
        bad = {**review, "expected_version": 1, "roots": [{"term": "absent", "category": "scene"}]}
        assert api.put(url + f"/{key}/review", json=bad).status_code == 422
        exported = api.get(url + f"/{key}/export")
        assert "'=1+1" in exported.text and "portable" in exported.text
        assert api.post(url, json={**request, "seller_start": "2026-09-01"}).status_code == 409
        assert api.post(url, json={**request, "text_only_acknowledged": False}).status_code == 422
        assert json.loads(api.get(url + f"/{key}/rows?kind=phrases").text)["total"] == 8
    with TestClient(create_app(db, storage_root=tmp_path)) as api:
        assert any(r["term"] == "portable" for r in api.get(url).json()[0]["review"]["roots"])
        # 不同项目不能读取本运行的分析、证据或提交审核。
        other = api.post("/api/v1/projects", json={"name": "other", "marketplace": "US"}).json()[
            "id"
        ]
        foreign = url.replace(base.split("/")[4], other)
        assert api.get(foreign + f"/{key}").status_code == 404
        assert api.get(foreign + f"/{key}/rows").status_code == 404
        assert (
            api.put(foreign + f"/{key}/review", json={**review, "expected_version": 1}).status_code
            == 404
        )
        original_page = CleaningService.page
        replaced = False

        def replace_after_read(
            service: CleaningService,
            project: UUID,
            run: UUID,
            analysis: UUID,
            kind: Literal["phrases", "frequencies"],
            offset: int,
            limit: int,
        ) -> CleaningPage:
            nonlocal replaced
            result = original_page(service, project, run, analysis, kind, offset, limit)
            if kind == "phrases" and not replaced:
                replaced = True
                # 在审核候选读取完毕、获得写锁之前，模拟另一请求提交新确认批次。
                with Repository(engine, tmp_path).connect() as conn:
                    record = conn.execute(
                        "SELECT * FROM third_party_imports WHERE run_id=? LIMIT 1", (str(run),)
                    ).fetchone()
                    assert record is not None
                    document = json.loads(cast(str, record[3]))
                    new_id = str(uuid4())
                    document["id"] = new_id
                    document["confirmed_at"] = (
                        datetime.now(UTC) + timedelta(seconds=1)
                    ).isoformat()
                    conn.execute(
                        "INSERT INTO third_party_imports VALUES(?,?,?,?,?,?)",
                        (
                            new_id,
                            str(run),
                            new_id,
                            json.dumps(document),
                            cast(str, record[4]),
                            cast(str, record[5]),
                        ),
                    )
            return result

        monkeypatch.setattr(CleaningService, "page", replace_after_read)
        rejected = api.put(url + f"/{key}/review", json={**review, "expected_version": 1})
        assert rejected.status_code == 409, rejected.text
        assert rejected.json()["error"]["code"] == "CLEANING_STALE"
        historical = api.get(url + f"/{key}").json()
        assert historical["stale"] is True and historical["version"] == 1
        regenerated = api.post(url, json=request)
        assert regenerated.status_code == 200
        assert regenerated.json()["id"] != key
        assert regenerated.json()["version"] == 0


def test_punctuation_numbers_do_not_manufacture_adjacent_terms(db: str, engine: Engine, ) -> None:
    rows, _ = analyze(["home 12 gym", "home-gym", "home / gym", "homes gym"])
    terms = {r.term: r for r in rows}
    assert "home gym" not in terms
    assert "homes gym" not in terms
    assert terms["home"].phrase_count == 3


def test_failed_classification_is_not_billed_again(db: str, engine: Engine, tmp_path: Path) -> None:
    calls = 0

    def fail(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("unknown outcome")

    repository = Repository(engine, tmp_path)
    repository.initialize()
    service = object.__new__(CleaningService)
    service.repository = repository
    service.model_client = QwenClient(
        QwenConfig(api_key="test"), transport=httpx.MockTransport(fail)
    )
    project = repository.create_project(ProjectCreate(name="分类计费保护", marketplace="US"))
    run = repository.create_run(
        project.id, ProductInput(marketplace="US", asin="B000000001"), "analysis"
    )
    analysis_id = "00000000-0000-0000-0000-000000000001"
    # 已记录的未知调用必须在读取任何业务依赖、再次访问模型之前就被阻止。
    with repository.connect() as conn:
        conn.execute(
            "INSERT INTO cleaning_analyses "
            "(id, run_id, fingerprint, document, phrases, frequencies) VALUES(?,?,?,?,?,?)",
            (analysis_id, str(run.id), "pending-test", "{}", "[]", "{}"),
        )
        conn.execute(
            "INSERT INTO cleaning_classifications VALUES(?,?,?)",
            (analysis_id, "outcome_pending", "{}"),
        )
    with pytest.raises(DomainError) as error:
        service._classification_available(  # pyright: ignore[reportPrivateUsage]
            UUID(analysis_id)
        )
    assert error.value.code == "CLEANING_CLASSIFICATION_OUTCOME_PENDING"
    assert calls == 0
