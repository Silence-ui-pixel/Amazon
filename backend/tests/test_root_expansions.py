from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import Engine
from test_verification_jobs import FakeSearch, page, setup

from app.domain.errors import DomainError
from app.domain.roots import RootCandidate
from app.domain.verification import VerificationInput
from app.services.verification import VerificationService


def configured(
    engine: Engine, tmp_path: Path, fake: FakeSearch
) -> tuple[VerificationService, UUID, UUID]:
    service, project, run = setup(engine, tmp_path, fake)
    roots = service.roots.get(project, run)
    roots.candidates = [
        RootCandidate(
            id=roots.candidates[0].id,
            text="wheel",
            translation="轮子",
            reason="主体品类",
            expansions=["gate wheel", "support wheel"],
        )
    ]
    with service.repository.connect() as conn:
        conn.execute(
            "UPDATE root_candidates SET document=? WHERE run_id=?",
            (roots.model_dump_json(), str(run)),
        )
    return service, project, run


def test_expansion_match_saves_atomic_root(engine: Engine, tmp_path: Path) -> None:
    fake = FakeSearch({("wheel", 1): page(None), ("gate wheel", 1): page(11)})
    service, p, r = configured(engine, tmp_path, fake)
    task = service.start(p, r, VerificationInput())
    service.execute(p, r, task.id)
    done = service.get(p, r, task.id)
    assert done.selection_saved and done.final
    assert done.items[0].text == "wheel"
    assert done.items[0].matched_query == "gate wheel"
    assert fake.calls == [("wheel", 1), ("gate wheel", 1)]
    selected = service.roots.get_selection(p, r)
    assert selected.candidate_ids == [done.items[0].candidate_id]
    assert service.roots.get(p, r).candidates[0].text == "wheel"


def test_direct_hit_skips_expansions(engine: Engine, tmp_path: Path) -> None:
    fake = FakeSearch({("wheel", 1): page(2)})
    service, p, r = configured(engine, tmp_path, fake)
    task = service.start(p, r, VerificationInput())
    service.execute(p, r, task.id)
    assert fake.calls == [("wheel", 1)]
    assert service.get(p, r, task.id).items[0].matched_query == "wheel"


def test_expanded_query_resume_keeps_base_and_query_checkpoints(
    engine: Engine, tmp_path: Path
) -> None:
    fake = FakeSearch(
        {
            ("wheel", 1): page(None),
            ("gate wheel", 1): page(None, next_page=True),
            ("gate wheel", 2): DomainError(502, "SEARCH_ACCESS_CHECK", "检查"),
        }
    )
    service, p, r = configured(engine, tmp_path, fake)
    task = service.start(p, r, VerificationInput())
    service.execute(p, r, task.id)
    assert service.get(p, r, task.id).status == "paused"
    fake.pages[("gate wheel", 2)] = page(3)
    service.resume(p, r, task.id)
    service.execute(p, r, task.id)
    assert fake.calls == [("wheel", 1), ("gate wheel", 1), ("gate wheel", 2), ("gate wheel", 2)]
    done = service.get(p, r, task.id)
    assert done.selection_saved and done.items[0].matched_query == "gate wheel"
    assert done.items[0].checked_pages == 3


@pytest.mark.parametrize(
    "code", ["SEARCH_ACCESS_CHECK", "SEARCH_NETWORK_TIMEOUT", "IMAGE_MATCH_UNCERTAIN"]
)
def test_failure_is_not_a_miss_and_does_not_expand(
    engine: Engine, tmp_path: Path, code: str
) -> None:
    fake = FakeSearch({("wheel", 1): DomainError(502, code, "未完成")})
    service, p, r = configured(engine, tmp_path, fake)
    task = service.start(p, r, VerificationInput())
    service.execute(p, r, task.id)
    assert fake.calls == [("wheel", 1)]
    assert not service.get(p, r, task.id).selection_saved


def test_all_queries_exhausted_does_not_save(engine: Engine, tmp_path: Path) -> None:
    fake = FakeSearch({(q, 1): page(None) for q in ("wheel", "gate wheel", "support wheel")})
    service, p, r = configured(engine, tmp_path, fake)
    task = service.start(p, r, VerificationInput())
    service.execute(p, r, task.id)
    done = service.get(p, r, task.id)
    assert done.final and not done.selection_saved
    assert done.items[0].completed_queries == ["wheel", "gate wheel", "support wheel"]
    assert done.items[0].status == "not_found_in_scope"


def test_waits_until_base_page_limit_before_expanding(engine: Engine, tmp_path: Path) -> None:
    fake = FakeSearch({("wheel", n): page(None, next_page=True) for n in (1, 2, 3)})
    fake.pages[("gate wheel", 1)] = page(11)
    service, p, r = configured(engine, tmp_path, fake)
    task = service.start(p, r, VerificationInput())
    service.execute(p, r, task.id)
    assert fake.calls == [("wheel", 1), ("wheel", 2), ("wheel", 3), ("gate wheel", 1)]
    assert service.get(p, r, task.id).selection_saved
