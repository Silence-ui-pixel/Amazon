"""Step 7 button must launch a durable, resumable model review without orchestration."""
import time
from pathlib import Path
from threading import Event

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from test_aba import auth_file, row
from test_aba_expansion import prepare, wait
from test_third_party import MAPPING, preview
from test_workflow_auto_late import fake_late_model

from app.adapters.aba import AbaClient
from app.domain.errors import DomainError
from app.main import create_app


def seed(api, base):
    for direction in ('forward', 'reverse'):
        task = api.post(base + '/aba-expansion', json={
            'direction': direction, 'week_end': '2026-10-17', 'max_rows': 100,
        }).json()
        assert wait(api, base + '/aba-expansion', task['id'])['status'] == 'succeeded'
    for direction, query in [('forward', 'cooling blanket'), ('reverse', 'B012345678'), ('reverse', 'B087654321')]:
        imported = preview(api, base + '/third-party', query, direction)
        response = api.post(base + f"/third-party/{imported['id']}/confirm", json={'mapping': MAPPING, 'share_unit': 'percent'})
        assert response.status_code == 200, response.text
    clean = api.post(base + '/cleaning', json={
        'aba_week_end': '2026-10-17', 'seller_start': '2026-10-01',
        'seller_end': '2026-10-31', 'text_only_acknowledged': True,
    }).json()
    response = api.put(base + f"/cleaning/{clean['id']}/review", json={
        'expected_version': 0, 'roots': [{'term': 'portable', 'category': 'preference'}],
        'exclusions': [{'keyword': '=1+1', 'reason': 'noise'}], 'reason': 'confirmed',
    })
    assert response.status_code == 200, response.text
    return {'cleaning_id': clean['id'], 'cleaning_version': 1}


def settled(api, url):
    for _ in range(200):
        response = api.get(url + '/auto-review')
        assert response.status_code == 200, response.text
        value = response.json()
        if value['status'] not in ('queued', 'running'):
            return value
        time.sleep(.02)
    raise AssertionError('review did not settle')


@pytest.mark.parametrize('mode', ['success', 'failure_retry', 'manual_preserved'])
def test_panel_model_review_without_workflow(db: str, engine: Engine, tmp_path: Path, mode: str):
    # Catches a button endpoint that only creates a report, blocks on the model,
    # duplicates model work on double click, or discards saved reviews on retry.
    base = prepare(db, engine, tmp_path)
    entered, release = Event(), Event()
    fail = [mode == 'failure_retry']
    def hook():
        entered.set()
        assert release.wait(10)
        if fail[0]:
            raise DomainError(504, 'MODEL_OUTCOME_UNKNOWN', 'model timeout')
    model = fake_late_model(traffic_hook=hook)
    aba = AbaClient(auth_file(tmp_path), transport=httpx.MockTransport(lambda _: httpx.Response(
        200, json={'reportsV2': [{'totalItems': 1, 'rows': [row('portable cooling blankets', 200001)]}]})))
    with TestClient(create_app(db, storage_root=tmp_path, model_client=model, aba_client=aba)) as api:
        data = seed(api, base)
        created = api.post(base + '/traffic?auto_review=true', json=data)
        assert created.status_code == 200, created.text
        report = created.json()
        url = base + f"/traffic/{report['id']}"
        try:
            assert entered.wait(2), 'Generating traffic never started model review'
            progress = api.get(url + '/auto-review')
            assert progress.status_code == 200, progress.text
            progress = progress.json()
            assert progress['status'] in ('queued', 'running')
            assert progress['required'] == 2 and progress['reviewed'] == 0
            duplicate = api.post(url + '/auto-review')
            assert duplicate.status_code == 202, duplicate.text
            assert duplicate.json()['attempt'] == progress['attempt']
            if mode == 'manual_preserved':
                saved = api.put(url + '/review', json={
                    'expected_version': 0, 'keyword': 'cooling blanket', 'decision': 'exclude',
                    'priority': 'low', 'reason': 'operator verified', 'evidence_note': 'manual facts',
                    'reviewer': 'operator',
                })
                assert saved.status_code == 200, saved.text
        finally:
            release.set()
        end = settled(api, url)
        if mode == 'failure_retry':
            assert end['status'] == 'failed' and end['error_code'] == 'MODEL_OUTCOME_UNKNOWN'
            assert not api.get(url).json()['complete']
            fail[0] = False
            assert api.post(url + '/auto-review').status_code == 202
            end = settled(api, url)
        assert end['status'] == 'succeeded', end
        assert end['reviewed'] == end['required'] == 2
        current = api.get(url).json()
        assert current['complete'] and current['version'] == 2
        audits = api.get(url + '/reviews').json()
        if mode == 'manual_preserved':
            own = next(r for r in audits if r['data']['keyword'] == 'cooling blanket')
            assert own['data']['decision'] == 'exclude' and own['data']['reviewer'] == 'operator'
        assert api.post(url + '/auto-review').json()['attempt'] == end['attempt']
        assert len(api.get(url + '/reviews').json()) == 2
        assert api.get(base + '/workflow/history').json() == []
        wrong = base.replace(base.split('/')[4], '00000000-0000-0000-0000-000000000001')
        assert api.post(wrong + f"/traffic/{report['id']}/auto-review").status_code == 404
    # Progress/results survive a new application instance, without a new model call.
    with TestClient(create_app(db, storage_root=tmp_path)) as api:
        restored = api.get(url + '/auto-review').json()
        assert restored['status'] == 'succeeded' and restored['reviewed'] == 2


def test_retry_only_requests_unsaved_keywords(db, engine, tmp_path, monkeypatch):
    from app.workflows import nodes_steps_6_8 as nodes
    monkeypatch.setattr(nodes, "TRAFFIC_BATCH", 1)
    monkeypatch.setattr(nodes, "_traffic_model_concurrency", lambda: 1)
    base = prepare(db, engine, tmp_path)
    calls = []
    def hook():
        calls.append(1)
        if len(calls) == 2:
            raise DomainError(504, "MODEL_OUTCOME_UNKNOWN", "model timeout")
    aba = AbaClient(auth_file(tmp_path), transport=httpx.MockTransport(lambda _: httpx.Response(
        200, json={"reportsV2": [{"totalItems": 1, "rows": [row("portable cooling blankets", 200001)]}]})))
    with TestClient(create_app(db, storage_root=tmp_path, model_client=fake_late_model(traffic_hook=hook), aba_client=aba)) as api:
        report = api.post(base + "/traffic?auto_review=true", json=seed(api, base)).json()
        url = base + f"/traffic/{report['id']}"
        end = settled(api, url)
        assert end["status"] == "failed" and end["reviewed"] == 1
        previous = api.get(url + "/reviews").json()
        assert len(previous) == 1
        assert api.post(url + "/auto-review").status_code == 202
        end = settled(api, url)
        assert end["status"] == "succeeded" and end["reviewed"] == 2
        assert len(calls) == 3, "Retry repeated a previously committed model decision"
        assert api.get(url + "/reviews").json()[0] == previous[0]


def test_interrupted_review_is_visible_and_resumable(db, engine, tmp_path):
    from datetime import UTC, datetime
    from sqlalchemy import text
    from app.domain.traffic import TrafficReviewProgress
    base = prepare(db, engine, tmp_path)
    aba = AbaClient(auth_file(tmp_path), transport=httpx.MockTransport(lambda _: httpx.Response(
        200, json={"reportsV2": [{"totalItems": 1, "rows": [row("portable cooling blankets", 200001)]}]})))
    with TestClient(create_app(db, storage_root=tmp_path, aba_client=aba)) as api:
        report = api.post(base + "/traffic", json=seed(api, base)).json()
        url = base + f"/traffic/{report['id']}"
    job = TrafficReviewProgress(analysis_id=report["id"], project_id=report["project_id"],
                               run_id=report["run_id"], status="running", attempt=1,
                               updated_at=datetime.now(UTC))
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO traffic_review_jobs VALUES(:id,:run,:status,:document)"),
                     {"id": report["id"], "run": report["run_id"], "status": job.status,
                      "document": job.model_dump_json()})
    with TestClient(create_app(db, storage_root=tmp_path, model_client=fake_late_model())) as api:
        progress = api.get(url + "/auto-review").json()
        assert progress["status"] == "failed"
        assert progress["error_code"] == "TRAFFIC_REVIEW_INTERRUPTED"
        assert api.post(url + "/auto-review").json()["attempt"] == 2
        assert settled(api, url)["status"] == "succeeded"


def test_parallel_panel_and_orchestration_share_review_lock(db, engine, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from uuid import UUID
    from app.domain.traffic import TrafficReport
    from app.workflows.nodes_steps_6_8 import review_traffic_report
    entered, release = Event(), Event()
    calls = []
    def hook():
        calls.append(1)
        entered.set()
        assert release.wait(10)
    base = prepare(db, engine, tmp_path)
    aba = AbaClient(auth_file(tmp_path), transport=httpx.MockTransport(lambda _: httpx.Response(
        200, json={"reportsV2": [{"totalItems": 1, "rows": [row("portable cooling blankets", 200001)]}]})))
    with TestClient(create_app(db, storage_root=tmp_path, model_client=fake_late_model(traffic_hook=hook), aba_client=aba)) as api:
        report = TrafficReport.model_validate(api.post(base + "/traffic", json=seed(api, base)).json())
        deps = api.app.state.workflow_service.deps
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(review_traffic_report, deps, report.project_id, report.run_id, report)
            try:
                assert entered.wait(2)
                second = pool.submit(review_traffic_report, deps, report.project_id, report.run_id, report)
            finally:
                release.set()
            assert sorted([first.result()[1], second.result()[1]]) == [0, 2]
        assert len(calls) == 1
