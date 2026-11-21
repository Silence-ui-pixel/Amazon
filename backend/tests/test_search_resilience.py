"""采集故障分类、有限重试和恢复语义；不访问外网。"""

from pathlib import Path

import pytest
from sqlalchemy import Engine
from test_verification_jobs import FakeSearch, page, setup

from app.domain.errors import DomainError
from app.domain.verification import SearchPage, VerificationInput
from app.services import amazon_search


def test_retry_policy_retries_transient_failure_without_repeating_success(db: str, ) -> None:
    retry = amazon_search.retry_search
    assert callable(retry), "采集器应提供有限重试策略"
    calls: list[int] = []
    waits: list[float] = []

    def attempt(number: int) -> SearchPage:
        calls.append(number)
        if number < 3:
            raise DomainError(502, "SEARCH_NETWORK_TIMEOUT", "连接超时")
        return page(2)

    result = retry(attempt, sleep=waits.append)
    assert calls == [1, 2, 3] and waits == [10, 30]
    assert result.cards[0].position == 2


def test_access_check_is_not_automatically_retried(db: str, ) -> None:
    retry = amazon_search.retry_search
    assert callable(retry)
    calls: list[int] = []

    def attempt(number: int) -> SearchPage:
        calls.append(number)
        raise DomainError(502, "SEARCH_ACCESS_CHECK", "需要人工处理")

    with pytest.raises(DomainError) as error:
        retry(attempt, sleep=lambda _: pytest.fail("检查页不能自动重试"))
    assert error.value.code == "SEARCH_ACCESS_CHECK" and calls == [1]


def test_checkpoint_pauses_all_roots_and_resume_does_not_recapture_completed_pages(
    engine: Engine, tmp_path: Path,
) -> None:
    fake = FakeSearch(
        {
            ("cup", 1): page(None, next_page=True),
            ("cup", 2): DomainError(502, "SEARCH_ACCESS_CHECK", "需要人工处理"),
            ("mug", 1): page(4),
        }
    )
    service, project, run = setup(engine, tmp_path, fake)
    task = service.start(project, run, VerificationInput())
    service.execute(project, run, task.id)
    paused = service.get(project, run, task.id)
    assert paused.status == "paused", "检查页应暂停整个采集任务"
    assert not paused.final and fake.calls == [("cup", 1), ("cup", 2)]
    assert paused.items[0].checked_pages == 1
    fake.pages[("cup", 2)] = page(1)
    resumed = service.resume(project, run, task.id)
    assert resumed.status == "queued"
    assert service.resume(project, run, task.id).status == "queued"
    service.execute(project, run, task.id)
    done = service.get(project, run, task.id)
    assert done.final and done.status == "completed"
    assert fake.calls == [("cup", 1), ("cup", 2), ("cup", 2), ("mug", 1)]


def test_resume_after_restart_keeps_matched_roots(
    db: str, engine: Engine, tmp_path: Path
) -> None:
    fake = FakeSearch(
        {("cup", 1): page(2), ("mug", 1): DomainError(502, "SEARCH_NETWORK_TIMEOUT", "超时")}
    )
    service, project, run = setup(engine, tmp_path, fake)
    task = service.start(project, run, VerificationInput())
    service.execute(project, run, task.id)
    fake.pages[("mug", 1)] = page(1)
    service.recover()
    service.resume(project, run, task.id)
    service.execute(project, run, task.id)
    assert fake.calls == [("cup", 1), ("mug", 1), ("mug", 1)]
    assert service.get(project, run, task.id).final


def test_retry_honors_retry_after_and_caps_retries(db: str, ) -> None:
    from app.services.search_policy import SearchFailure, retry_search

    waits: list[float] = []
    calls: list[int] = []

    def limited(attempt: int) -> SearchPage:
        calls.append(attempt)
        raise SearchFailure("SEARCH_RATE_LIMITED", "限流", retry_after=45)

    with pytest.raises(SearchFailure):
        retry_search(limited, sleep=waits.append)
    assert calls == [1, 2, 3] and waits == [45, 45]


def test_unknown_model_result_cannot_resume(db: str, engine: Engine, tmp_path: Path) -> None:
    fake = FakeSearch({("cup", 1): page(1), ("mug", 1): page(2)})
    service, project, run = setup(engine, tmp_path, fake)
    task = service.start(project, run, VerificationInput())
    task.status = "interrupted"
    task.model_outcome_pending = True
    service.save(task)
    with pytest.raises(DomainError) as error:
        service.resume(project, run, task.id)
    assert error.value.code == "MODEL_OUTCOME_PENDING" and not fake.calls


def test_resume_validates_scope_before_signaling_manual_window(
    db: str, engine: Engine, tmp_path: Path
) -> None:
    from threading import Event
    from uuid import uuid4

    fake = FakeSearch({})
    service, project, run = setup(engine, tmp_path, fake)
    task = service.start(project, run, VerificationInput())
    event = Event()
    service.review_events[task.id] = event
    with pytest.raises(DomainError):
        service.resume(uuid4(), run, task.id)
    assert not event.is_set()
    service.review_events.clear()


def test_unprocessed_page_is_reused_after_interruption(
    db: str, engine: Engine, tmp_path: Path
) -> None:
    from app.domain.verification import PageCheckpoint

    fake = FakeSearch({("mug", 1): page(3)})
    service, project, run = setup(engine, tmp_path, fake)
    task = service.start(project, run, VerificationInput())
    task.status = "interrupted"
    task.items[0].pages.append(PageCheckpoint(number=1, result=page(1)))
    service.save(task)
    service.resume(project, run, task.id)
    service.execute(project, run, task.id)
    assert fake.calls == [("mug", 1)]
    assert service.get(project, run, task.id).final


def test_resume_api_is_idempotent_and_private_state_is_not_evidence(
    db: str, engine: Engine, tmp_path: Path
) -> None:
    from fastapi.testclient import TestClient

    from app.main import create_app

    fake = FakeSearch({})
    service, project, run = setup(engine, tmp_path, fake)
    task = service.start(project, run, VerificationInput())
    task.status = "paused"
    service.save(task)
    base = f"/api/v1/projects/{project}/pipeline-runs/{run}/root-verifications/{task.id}"
    app = create_app(db, storage_root=tmp_path, search_provider=fake, auto_execute=False)
    with TestClient(app) as client:
        assert client.post(base + "/resume").json()["status"] == "queued"
        assert client.post(base + "/resume").json()["status"] == "queued"
        assert client.get(base + "/evidence/browser-state.json").status_code == 404
        assert client.post(base + "/review").status_code == 409
    service.close()
