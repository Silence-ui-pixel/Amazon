"""离线采集器验证全候选执行、失败隔离、幂等和人工选择优先。"""

from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.db.repository import Repository
from app.domain.errors import DomainError
from app.domain.roots import RootCandidate, RootCandidates, RootSelectionInput
from app.domain.schemas import ProductInput, ProjectCreate
from app.domain.verification import SearchCard, SearchPage, VerificationInput
from app.main import create_app
from app.services.amazon_search import SearchSession
from app.services.images import ImageService
from app.services.product_understanding import ProductUnderstandingService
from app.services.roots import RootService
from app.services.verification import VerificationService

TARGET = "B0ABCDEF12"


class FakeSearch:
    def __init__(self, pages: dict[tuple[str, int], SearchPage | DomainError]) -> None:
        self.pages = pages
        self.calls: list[tuple[str, int]] = []

    @contextmanager
    def open(self, marketplace: str, evidence_dir: Path) -> Generator[SearchSession]:
        yield self

    def search(self, keyword: str, page_number: int, evidence_name: str) -> SearchPage:
        self.calls.append((keyword, page_number))
        result = self.pages[(keyword, page_number)]
        if isinstance(result, DomainError):
            raise result
        return result


def page(position: int | None, *, next_page: bool = False, region: str = "US") -> SearchPage:
    return SearchPage(
        region=region,
        cards=[] if position is None else [SearchCard(asin=TARGET, position=position)],
        next_page=next_page,
        url="https://www.amazon.com/s?k=cup",
    )


def setup(
    engine: Engine, tmp: Path, provider: FakeSearch, *, asin: bool = True
) -> tuple[VerificationService, UUID, UUID]:
    repo = Repository(engine, tmp)
    repo.initialize()
    project = repo.create_project(ProjectCreate(name="测试", marketplace="US"))
    images = ImageService(repo, tmp / "uploads")
    if asin:
        data = ProductInput(marketplace="US", asin=TARGET)
    else:
        from io import BytesIO

        from PIL import Image

        output = BytesIO()
        Image.new("RGB", (5, 5), "red").save(output, format="PNG")
        asset = images.save(project.id, "main.png", output.getvalue(), "image")
        data = ProductInput(marketplace="US", image_ids=[asset.id])
    run = repo.create_run(project.id, data, "run")
    roots = RootCandidates(
        project_id=project.id,
        run_id=run.id,
        model="test",
        marketplace="US",
        language="en",
        created_at=datetime.now(UTC),
        warnings=[],
        candidates=[
            RootCandidate(id=uuid4(), text=t, translation="杯子", reason="测试")
            for t in ("cup", "mug")
        ],
    )
    with repo.connect() as conn:
        conn.execute(
            "INSERT INTO root_candidates VALUES (?,'succeeded',?)",
            (str(run.id), roots.model_dump_json()),
        )
    service = VerificationService(
        repo,
        RootService(repo, ProductUnderstandingService(repo, images, None), None),
        images,
        provider,
        auto_execute=False,
    )
    return service, project.id, run.id


def test_checks_all_keywords_selects_page_then_position_and_is_idempotent(db: str, engine: Engine, tmp_path: Path) -> None:
    fake = FakeSearch(
        {("cup", 1): page(None, next_page=True), ("cup", 2): page(1), ("mug", 1): page(8)}
    )
    service, p, r = setup(engine, tmp_path, fake)
    task = service.start(p, r, VerificationInput())
    assert service.start(p, r, VerificationInput()).id == task.id
    service.execute(p, r, task.id)
    result = service.get(p, r, task.id)
    assert fake.calls == [("cup", 1), ("cup", 2), ("mug", 1)]
    assert result.final and result.selection_saved
    assert result.best_candidate_ids == [result.items[1].candidate_id]
    selection = service.roots.get_selection(p, r)
    assert selection.source == "automatic" and selection.verification_id == task.id
    service.execute(p, r, task.id)
    assert len(fake.calls) == 3
    # 相同候选人工保存也必须升级为 manual，防止后续任务覆盖。
    manual = service.roots.select(p, r, RootSelectionInput(candidate_ids=selection.candidate_ids))
    assert manual.source == "manual"


def test_manual_subset_only_searches_selected_and_does_not_autosave(db: str, engine: Engine, tmp_path: Path) -> None:
    fake = FakeSearch({("mug", 1): page(2)})
    service, project, run = setup(engine, tmp_path, fake)
    roots = service.roots.get(project, run)
    selected = roots.candidates[1].id
    request = VerificationInput.model_validate({"candidate_ids": [str(selected)]})
    task = service.start(project, run, request)
    assert [item.candidate_id for item in task.items] == [selected]
    assert service.start(project, run, request).id == task.id
    other = service.start(
        project,
        run,
        VerificationInput.model_validate({"candidate_ids": [str(roots.candidates[0].id)]}),
    )
    assert other.id != task.id
    service.execute(project, run, task.id)
    result = service.get(project, run, task.id)
    assert fake.calls == [("mug", 1)]
    assert result.final and not result.selection_saved
    assert result.best_candidate_ids == [selected]
    with pytest.raises(DomainError) as error:
        service.roots.get_selection(project, run)
    assert error.value.status_code == 404


def test_manual_subset_rejects_foreign_and_duplicate_ids(db: str, engine: Engine, tmp_path: Path) -> None:
    from pydantic import ValidationError

    service, project, run = setup(engine, tmp_path, FakeSearch({}))
    with pytest.raises(ValidationError):
        VerificationInput.model_validate({"candidate_ids": []})
    chosen = service.roots.get(project, run).candidates[0].id
    with pytest.raises(ValidationError):
        VerificationInput.model_validate({"candidate_ids": [str(chosen), str(chosen)]})
    with pytest.raises(DomainError) as error:
        service.start(
            project, run, VerificationInput.model_validate({"candidate_ids": [str(uuid4())]})
        )
    assert error.value.status_code == 422


@pytest.mark.parametrize("mode", ["failure", "region", "not_found", "manual", "tie"])
def test_incomplete_not_found_manual_and_ties(db: str, engine: Engine, tmp_path: Path, mode: str) -> None:
    pages: dict[tuple[str, int], SearchPage | DomainError] = {
        ("cup", 1): page(3),
        ("mug", 1): page(1),
    }
    if mode == "failure":
        pages[("mug", 1)] = DomainError(502, "BLOCKED", "需要验证")
    if mode == "region":
        pages[("mug", 1)] = page(1, region="CA")
    if mode == "not_found":
        pages = {("cup", 1): page(None), ("mug", 1): page(None)}
    if mode == "tie":
        pages[("mug", 1)] = page(3)
    service, p, r = setup(engine, tmp_path, FakeSearch(pages))
    ids: list[UUID] = []
    if mode == "manual":
        ids = [service.roots.get(p, r).candidates[0].id]
        service.roots.select(p, r, RootSelectionInput(candidate_ids=ids))
    task = service.start(p, r, VerificationInput())
    service.execute(p, r, task.id)
    result = service.get(p, r, task.id)
    if mode in ("failure", "region"):
        assert not result.final and not result.selection_saved
        assert result.items[1].status == "failed"
    elif mode == "not_found":
        assert result.final and result.best_candidate_ids == [] and not result.selection_saved
    elif mode == "manual":
        assert result.final and not result.selection_saved
        assert service.roots.get_selection(p, r).candidate_ids == ids
    else:
        assert result.final and len(result.best_candidate_ids) == 2


def test_api_scope_missing_reference_and_restart(db: str, engine: Engine, tmp_path: Path) -> None:
    fake = FakeSearch({})
    service, p, r = setup(engine, tmp_path, fake, asin=False)
    task = service.start(p, r, VerificationInput())
    assert task.status == "needs_reference" and fake.calls == []
    image = service.repository.get_run(p, r).input.image_ids[0]
    queued = service.start(p, r, VerificationInput(reference_image_id=image))
    with TestClient(create_app(db, storage_root=tmp_path, search_provider=fake, auto_execute=False)) as client:
        base = f"/api/v1/projects/{p}/pipeline-runs/{r}/root-verifications"
        assert client.get(base + f"/{queued.id}").json()["status"] == "interrupted"
        assert client.post(base, json={"reference_image_id": str(image)}).json()["id"] == str(
            queued.id
        )
        assert client.get(base.replace(str(p), str(uuid4())) + f"/{task.id}").status_code == 404
        assert client.get(base + f"/{task.id}/evidence/secret.png").status_code == 404
        assert client.post(base, json={"reference_image_id": str(uuid4())}).status_code == 422


def test_max_three_pages_does_not_stop_other_candidates(db: str, engine: Engine, tmp_path: Path) -> None:
    fake = FakeSearch(
        {
            (word, number): page(None, next_page=True)
            for word in ("cup", "mug")
            for number in (1, 2, 3)
        }
    )
    service, p, r = setup(engine, tmp_path, fake)
    task = service.start(p, r, VerificationInput())
    service.execute(p, r, task.id)
    assert len(fake.calls) == 6
    assert all(
        i.checked_pages == 3 and i.status == "not_found_in_scope"
        for i in service.get(p, r, task.id).items
    )


def test_browser_start_failure_persists_each_failure(db: str, engine: Engine, tmp_path: Path) -> None:
    class Broken(FakeSearch):
        @contextmanager
        def open(self, marketplace: str, evidence_dir: Path) -> Generator[SearchSession]:
            raise DomainError(503, "BROWSER", "浏览器未安装")
            yield self

    service, p, r = setup(engine, tmp_path, Broken({}))
    task = service.start(p, r, VerificationInput())
    service.execute(p, r, task.id)
    result = service.get(p, r, task.id)
    assert result.status == "incomplete" and all(i.status == "failed" for i in result.items)
