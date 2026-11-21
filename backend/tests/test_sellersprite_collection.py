"""浏览器采集任务必须按上游快照校验并只保存预览。"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import Engine
from test_aba_expansion import prepare

from app.main import create_app


def test_collection_contract_supports_sop_methods_and_legacy_json(db: str, ) -> None:
    from app.domain.sellersprite import BrowserCollectionInput

    legacy = BrowserCollectionInput.model_validate(
        {"direction": "reverse", "query": "B0BGP8RBWR", "period": "last_30_days"}
    )
    assert legacy.method == "keyword_reverse"
    assert legacy.queries == ["B0BGP8RBWR"]
    assert legacy.variant_scope is None

    extension = BrowserCollectionInput.model_validate(
        {
            "direction": "reverse",
            "query": "",
            "method": "traffic_extension",
            "queries": ["B0BGP8RBWR", "B01G643EU6"],
            "variant_scope": "best_selling",
            "period": "last_30_days",
        }
    )
    assert extension.query == "B0BGP8RBWR,B01G643EU6"
    assert extension.queries == ["B0BGP8RBWR", "B01G643EU6"]


@pytest.mark.parametrize(
    "payload",
    [
        {
            "direction": "reverse",
            "query": "",
            "method": "traffic_extension",
            "queries": [],
            "variant_scope": "all",
        },
        {
            "direction": "reverse",
            "query": "",
            "method": "traffic_extension",
            "queries": [f"B{i:09d}" for i in range(21)],
            "variant_scope": "all",
        },
        {
            "direction": "reverse",
            "query": "",
            "method": "traffic_extension",
            "queries": ["B0BGP8RBWR", "B0BGP8RBWR"],
            "variant_scope": "current",
        },
        {
            "direction": "forward",
            "query": "cooling blanket",
            "method": "keyword_mining",
            "queries": ["cooling blanket"],
            "variant_scope": "all",
        },
    ],
)
def test_collection_contract_rejects_invalid_sop_combinations(db: str, payload: dict[str, object]) -> None:
    from app.domain.sellersprite import BrowserCollectionInput

    with pytest.raises(ValidationError):
        BrowserCollectionInput.model_validate(payload)


def test_browser_job_requires_saved_query_and_restores(
    db: str, engine: Engine, tmp_path: Path
) -> None:
    base = prepare(db, engine, tmp_path)
    with TestClient(create_app(db, storage_root=tmp_path)) as client:
        response = client.post(
            base + "/third-party/browser-jobs",
            json={
                "direction": "forward",
                "query": "not saved",
                "period": "last_30_days",
            },
        )
        assert response.status_code == 422
        assert client.get(base + "/third-party/browser-jobs").json() == []


def test_batch_extension_only_accepts_saved_competitors(
    db: str, engine: Engine, tmp_path: Path
) -> None:
    base = prepare(db, engine, tmp_path)
    with TestClient(create_app(db, storage_root=tmp_path, auto_execute=False)) as client:
        endpoint = base + "/third-party/browser-jobs"
        accepted = client.post(
            endpoint,
            json={
                "direction": "reverse",
                "query": "",
                "method": "traffic_extension",
                "queries": ["B012345678", "B087654321"],
                "variant_scope": "current",
                "period": "last_30_days",
            },
        )
        assert accepted.status_code == 202, accepted.text
        assert accepted.json()["request"]["query"] == "B012345678,B087654321"
        rejected = client.post(
            endpoint,
            json={
                "direction": "reverse",
                "query": "",
                "method": "traffic_extension",
                "queries": ["B012345678", "B000000000"],
                "variant_scope": "all",
                "period": "last_30_days",
            },
        )
        assert rejected.status_code == 422


def test_import_original_download_before_confirmation(
    db: str, engine: Engine, tmp_path: Path
) -> None:
    from test_third_party import CSV, preview

    base = prepare(db, engine, tmp_path)
    with TestClient(create_app(db, storage_root=tmp_path)) as client:
        report = preview(client, base + "/third-party", "cooling blanket")
        response = client.get(base + "/third-party/" + str(report["id"]) + "/original")
        assert response.status_code == 200
        assert response.content == CSV


def test_browser_job_downloads_to_preview_and_keeps_original(
    db: str, engine: Engine, tmp_path: Path, monkeypatch: object
) -> None:
    import time

    import pytest

    from app.adapters.sellersprite_browser import SellerspriteBrowserDownloader

    assert isinstance(monkeypatch, pytest.MonkeyPatch)
    raw = b"keyword,monthlysearches\ncooling blanket,1200\n"
    calls: list[str] = []

    def download(
        self: SellerspriteBrowserDownloader, request: object, target: Path
    ) -> tuple[str, bytes, int]:
        from app.domain.sellersprite import BrowserCollectionInput

        assert isinstance(request, BrowserCollectionInput)
        target.write_bytes(raw)
        assert request.period == "last_30_days"
        calls.append(request.query)
        return "fixture.csv", raw, 1

    monkeypatch.setattr(SellerspriteBrowserDownloader, "download", download)
    base = prepare(db, engine, tmp_path)
    with TestClient(create_app(db, storage_root=tmp_path)) as client:
        endpoint = base + "/third-party/browser-jobs"
        job = client.post(
            endpoint,
            json={"direction": "forward", "query": "cooling blanket", "period": "last_30_days"},
        )
        assert job.status_code == 202, job.text
        jobs = client.get(endpoint).json()
        for _ in range(100):
            jobs = client.get(endpoint).json()
            if jobs[0]["status"] not in ("queued", "running"):
                break
            time.sleep(0.01)
        assert jobs[0]["status"] == "succeeded", jobs
        report_id = jobs[0]["report_id"]
        workspace = client.get(base + "/third-party").json()
        assert workspace["reports"][0]["status"] == "preview"
        assert workspace["reports"][0]["metadata"]["period_start"] is None
        assert workspace["reports"][0]["metadata"]["period_end"] is None
        assert workspace["reports"][0]["metadata"]["period_kind"] == "last_30_days"
        assert workspace["reports"][0]["metadata"]["collected_at"]
        assert workspace["complete"] is False
        assert client.get(base + "/third-party/" + report_id + "/original").content == raw
        assert calls == ["cooling blanket"]


def _run_job(client: TestClient, endpoint: str) -> list[dict[str, object]]:
    import time

    assert (
        client.post(
            endpoint,
            json={"direction": "forward", "query": "cooling blanket", "period": "last_30_days"},
        ).status_code
        == 202
    )
    jobs = client.get(endpoint).json()
    for _ in range(100):
        jobs = client.get(endpoint).json()
        if jobs[0]["status"] not in ("queued", "running"):
            break
        time.sleep(0.01)
    return jobs


def test_truncated_export_imports_with_marker(
    db: str, engine: Engine, tmp_path: Path, monkeypatch: object
) -> None:
    """供应商单次上限导致的截断可以导入，但必须标记为截断，不能冒充完整。"""
    import pytest

    from app.adapters.sellersprite_browser import SellerspriteBrowserDownloader

    assert isinstance(monkeypatch, pytest.MonkeyPatch)

    def download(
        self: SellerspriteBrowserDownloader, request: object, target: Path
    ) -> tuple[str, bytes, int]:
        raw = b"keyword" + bytes([10]) + b"cooling blanket" + bytes([10])
        return "fixture.csv", raw, 9559

    monkeypatch.setattr(SellerspriteBrowserDownloader, "download", download)
    base = prepare(db, engine, tmp_path)
    with TestClient(create_app(db, storage_root=tmp_path)) as client:
        jobs = _run_job(client, base + "/third-party/browser-jobs")
        assert jobs[0]["status"] == "succeeded", jobs
        report = client.get(base + "/third-party").json()["reports"][0]
        assert report["truncated"] is True
        assert report["complete"] is False
        assert report["metadata"]["source_total"] == 9559
        assert report["metadata"]["export_complete"] is False


def test_export_larger_than_page_total_never_imports(
    db: str, engine: Engine, tmp_path: Path, monkeypatch: object
) -> None:
    import pytest

    from app.adapters.sellersprite_browser import SellerspriteBrowserDownloader

    assert isinstance(monkeypatch, pytest.MonkeyPatch)

    def download(
        self: SellerspriteBrowserDownloader, request: object, target: Path
    ) -> tuple[str, bytes, int]:
        raw = b"keyword" + bytes([10]) + b"cooling blanket" + bytes([10])
        return "fixture.csv", raw, 0

    monkeypatch.setattr(SellerspriteBrowserDownloader, "download", download)
    base = prepare(db, engine, tmp_path)
    with TestClient(create_app(db, storage_root=tmp_path)) as client:
        jobs = _run_job(client, base + "/third-party/browser-jobs")
        assert jobs[0]["status"] == "failed"
        assert not client.get(base + "/third-party").json()["reports"]
