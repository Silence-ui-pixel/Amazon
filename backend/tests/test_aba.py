"""ABA 迁入契约测试；仅以传输替身隔离真实商家请求。"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.db.repository import Repository
from app.domain.errors import DomainError
from app.main import create_app


def auth_file(tmp_path: Path) -> Path:
    path = tmp_path / "auth.json"
    path.write_text(json.dumps({"cookie": "session=test-secret", "ua": "test-browser"}))
    return path


def row(term: str, rank: object = 123) -> dict[str, object]:
    return {
        "st-search-term": term,
        "st-search-frequency": rank,
        "st-top-asin-1": "B0G3GRFJYB",
        "st-top-asin-click-share-1": 12.5,
        "st-top-asin-conversion-share-1": 5,
    }


def test_weekly_protocol_and_atomic_root_preserved(db: str, engine: Engine, tmp_path: Path) -> None:
    from app.adapters.aba import AbaClient

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert request.url.host == "sellercentral.amazon.com"
        assert request.headers["cookie"] == "session=test-secret"
        assert {"id": "search-term-freeform", "value": "rack", "valueType": None} in body[
            "filterSelections"
        ]
        assert {"id": "weekly-week", "value": "2026-10-17", "valueType": "weekly"} in body[
            "filterSelections"
        ]
        assert body["selectedCountries"] == ["us"]
        assert body["reportOperations"][0]["pageNumber"] == 1
        assert body["reportOperations"][0]["pageSize"] == 100
        assert body["reportOperations"][0]["ascending"] is True
        return httpx.Response(200, json={"reportsV2": [{"totalItems": 1, "rows": [row("rack")]}]})

    result = AbaClient(auth_file(tmp_path), transport=httpx.MockTransport(handle)).fetch(
        "rack", "2026-10-17", max_rows=1000
    )
    assert result.rows[0].keyword == "rack"
    assert result.rows[0].search_frequency_rank == 123
    assert result.rows[0].products[0].click_share == 0.125
    assert result.rows[0].products[0].conversion_share == 0.05
    assert result.truncated is False
    assert result.pages == 1


def test_pagination_honors_exact_limit_and_marks_partial(db: str, engine: Engine, tmp_path: Path) -> None:
    from app.adapters.aba import AbaClient

    def handle(request: httpx.Request) -> httpx.Response:
        page = json.loads(request.content)["reportOperations"][0]["pageNumber"]
        return httpx.Response(
            200,
            json={
                "reportsV2": [
                    {
                        "totalItems": 250,
                        "rows": [row(f"rack {page}-{i}", page * 100 + i) for i in range(100)],
                    }
                ]
            },
        )

    result = AbaClient(auth_file(tmp_path), transport=httpx.MockTransport(handle)).fetch(
        "rack", "2026-10-17", max_rows=125
    )
    assert len(result.rows) == 125
    assert result.total == 250
    assert result.pages == 2
    assert result.truncated
    assert result.stop_reason == "row_limit"


FAILURES: list[tuple[int, object, str]] = [
    (
        200,
        {"reportsV2": [{"totalItems": 1, "rows": [row("rack"), row("shoe rack")]}]},
        "ABA_INVALID_RESPONSE",
    ),
    (401, None, "ABA_AUTH_REQUIRED"),
    (403, None, "ABA_ACCESS_DENIED"),
    (429, None, "ABA_RATE_LIMITED"),
    (200, None, "ABA_INVALID_RESPONSE"),
    (200, {}, "ABA_INVALID_RESPONSE"),
    (200, {"reportsV2": [{"rows": []}]}, "ABA_INVALID_RESPONSE"),
    (200, {"reportsV2": [{"totalItems": 1, "rows": [row("rack", 0)]}]}, "ABA_INVALID_RESPONSE"),
]


@pytest.mark.parametrize(("status", "payload", "code"), FAILURES)
def test_external_failure_never_becomes_empty_success(
    tmp_path: Path, status: int, payload: object, code: str
) -> None:
    from app.adapters.aba import AbaClient

    def handle(_request: httpx.Request) -> httpx.Response:
        return (
            httpx.Response(status, json=payload)
            if payload is not None
            else httpx.Response(status, text="<html>login</html>")
        )

    with pytest.raises(DomainError) as error:
        AbaClient(auth_file(tmp_path), transport=httpx.MockTransport(handle), attempts=1).fetch(
            "rack", "2026-10-17"
        )
    assert error.value.code == code
    assert "test-secret" not in error.value.message


def test_short_page_with_more_total_is_not_complete(db: str, engine: Engine, tmp_path: Path) -> None:
    from app.adapters.aba import AbaClient

    result = AbaClient(
        auth_file(tmp_path),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, json={"reportsV2": [{"totalItems": 20, "rows": [row("rack")]}]}
            )
        ),
    ).fetch("rack", "2026-10-17")
    assert result.truncated
    assert result.stop_reason == "short_page"


def test_overlapping_rows_cannot_be_counted_as_complete(db: str, engine: Engine, tmp_path: Path) -> None:
    from app.adapters.aba import AbaClient

    def handle(request: httpx.Request) -> httpx.Response:
        page = json.loads(request.content)["reportOperations"][0]["pageNumber"]
        start = (page - 1) * 50
        return httpx.Response(
            200,
            json={
                "reportsV2": [
                    {
                        "totalItems": 200,
                        "rows": [row(f"term {i}", i + 1) for i in range(start, start + 100)],
                    }
                ]
            },
        )

    result = AbaClient(auth_file(tmp_path), transport=httpx.MockTransport(handle)).fetch(
        "term", "2026-10-17"
    )
    assert result.truncated
    assert result.stop_reason == "repeated_page"
    assert len({item.keyword for item in result.rows}) == 150
    assert result.fetched == 200


def test_temporary_error_retries_but_auth_does_not(db: str, engine: Engine, tmp_path: Path) -> None:
    from app.adapters.aba import AbaClient

    statuses = iter([503, 200])

    def handle(_request: httpx.Request) -> httpx.Response:
        status = next(statuses)
        return httpx.Response(status, json={"reportsV2": [{"totalItems": 0, "rows": []}]})

    result = AbaClient(
        auth_file(tmp_path), transport=httpx.MockTransport(handle), retry_delay=0
    ).fetch("rack", "2026-10-17")
    assert result.total == 0
    assert result.truncated is False


def seed_run(client: TestClient, engine: Engine, tmp_path: Path) -> tuple[str, str, str]:
    from app.domain.roots import RootCandidate, RootCandidates, RootSelection

    project = client.post("/api/v1/projects", json={"name": "ABA", "marketplace": "US"}).json()[
        "id"
    ]
    run = client.post(
        f"/api/v1/projects/{project}/pipeline-runs",
        json={"marketplace": "US", "asin": "B0G3GRFJYB"},
        headers={"Idempotency-Key": "aba-run"},
    ).json()["id"]
    candidate = RootCandidate(id=uuid4(), text="rack", translation="架子", reason="商品类型")
    candidates = RootCandidates(
        project_id=project,
        run_id=run,
        model="test",
        marketplace="US",
        language="en",
        created_at=datetime.now(UTC),
        candidates=[candidate],
        warnings=[],
    )
    selection = RootSelection(
        project_id=project, run_id=run, candidate_ids=[candidate.id], confirmed_at=datetime.now(UTC)
    )
    with Repository(engine, tmp_path).connect() as conn:
        conn.execute(
            "INSERT INTO root_candidates VALUES (?, 'succeeded', ?)",
            (run, candidates.model_dump_json()),
        )
        conn.execute(
            "INSERT INTO root_selections VALUES (?, ?)", (run, selection.model_dump_json())
        )
    return project, run, str(candidate.id)


def test_api_persists_reuses_cache_and_isolates_projects(db: str, engine: Engine, tmp_path: Path) -> None:
    from app.adapters.aba import AbaClient

    calls: list[bytes] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.content)
        return httpx.Response(200, json={"reportsV2": [{"totalItems": 1, "rows": [row("rack")]}]})

    provider = AbaClient(auth_file(tmp_path), transport=httpx.MockTransport(handle))
    report_id = ""
    with TestClient(create_app(db, storage_root=tmp_path, aba_client=provider)) as client:
        project, run, _ = seed_run(client, engine, tmp_path)
        url = f"/api/v1/projects/{project}/pipeline-runs/{run}/aba-reports"
        request = {"week_end": "2026-10-17", "max_rows": 100}
        first = client.post(url, json=request)
        assert first.status_code == 200, first.text
        saved = first.json()
        report_id = saved["id"]
        assert "raw_rows" not in saved["reports"][0]
        assert "test-secret" not in first.text
        with Repository(engine, tmp_path).connect() as conn:
            evidence = conn.execute(
                "SELECT raw_document FROM aba_reports WHERE id=?", (report_id,)
            ).fetchone()
        assert json.loads(evidence[0])[0]["rows"][0]["st-search-frequency"] == 123
        assert saved["reports"][0]["rows"][0]["keyword"] == "rack"
        assert saved["week_start"] == "2026-10-11"
        assert saved["roots_snapshot"] == ["rack"]
        assert saved["source"] == "sellercentral_internal"
        assert client.post(url, json=request).json()["id"] == report_id
        assert len(calls) == 1
        assert client.get(url + "/" + report_id).json()["stale"] is False
        other = client.post("/api/v1/projects", json={"name": "other", "marketplace": "US"}).json()[
            "id"
        ]
        assert client.get(url.replace(project, other) + "/" + report_id).status_code == 404
        assert client.post(url.replace(project, other), json=request).status_code == 404
        assert client.post(url, json={"week_end": "2026-10-16"}).status_code == 422
        future = datetime.now(UTC).date() + timedelta(days=30)
        future += timedelta(days=(5 - future.weekday()) % 7)
        assert client.post(url, json={"week_end": future.isoformat()}).status_code == 422
        with Repository(engine, tmp_path).connect() as conn:
            conn.execute("DELETE FROM root_selections WHERE run_id=?", (run,))
        assert client.get(url + "/" + report_id).json()["stale"] is True
        assert client.post(url, json=request).status_code == 409
    with TestClient(create_app(db, storage_root=tmp_path, aba_client=provider)) as client:
        assert client.get(url + "/" + report_id).json()["id"] == report_id
        assert len(calls) == 1


def test_missing_credentials_and_unsupported_site_fail_early(db: str, engine: Engine, tmp_path: Path) -> None:
    from app.adapters.aba import AbaClient

    with pytest.raises(DomainError) as error:
        AbaClient(tmp_path / "missing.json").fetch("rack", "2026-10-17")
    assert error.value.code == "ABA_AUTH_REQUIRED"
    with pytest.raises(DomainError) as error:
        AbaClient(auth_file(tmp_path)).fetch("rack", "2026-10-17", country="de")
    assert error.value.code == "ABA_SITE_UNSUPPORTED"
