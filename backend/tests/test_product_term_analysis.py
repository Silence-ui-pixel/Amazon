"""完整商品词分析任务：有效分母、低样本可信度、暂停恢复与缓存复用。"""

import json
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine

from app.db.repository import Repository
from app.domain.asin_product import ProductSnapshot
from app.domain.errors import DomainError
from app.domain.product_terms import ProductProfile, ProductProfileRecord
from app.domain.roots import RootCandidate, RootCandidates
from app.domain.schemas import ProductInput, ProjectCreate
from app.domain.verification import SearchCard, SearchPage
from app.services.amazon_search import SearchProvider
from app.services.product_profiles import ProductProfileService
from app.services.product_term_analysis import ProductTermAnalysisService
from app.services.product_understanding import ProductUnderstandingService
from app.services.qwen import QwenClient, QwenConfig
from app.services.roots import RootService
from app.services.search_policy import SearchFailure


class OneProductSearch:
    def search(self, keyword: str, page_number: int, evidence_name: str) -> SearchPage:
        return SearchPage(
            region="New York 10001",
            cards=[SearchCard(asin="B000000002", position=1)],
            next_page=False,
        )

    def product(self, asin: str) -> ProductSnapshot:
        return ProductSnapshot(asin=asin, title="Gate wheel", price=Decimal("20"), currency="USD")

    @contextmanager
    def open(self, marketplace: str, evidence_dir: Path) -> Generator["OneProductSearch", None, None]:
        yield self


def test_analysis_scores_all_candidates_and_auto_selects_highest(
    engine: Engine, tmp_path: Path
) -> None:
    repository = Repository(engine, tmp_path)
    repository.initialize()
    project = repository.create_project(ProjectCreate(name="商品词", marketplace="US"))
    run = repository.create_run(
        project.id, ProductInput(marketplace="US", asin="B000000001"), "analysis"
    )
    reference = ProductProfile(
        purpose=["支撑大门"], style_structure=["夹具与轮子"], specifications=["户外"],
        price=Decimal("20"), currency="USD", package_quantity=Decimal("1"), unit_basis="each",
        price_source="asin_current",
    )
    profile = ProductProfileRecord(
        project_id=project.id, run_id=run.id, version=1, status="confirmed", profile=reference
    )
    candidate = RootCandidate(
        id=uuid4(), text="gate wheel", translation="大门轮", reason="同类商品"
    )
    roots_record = RootCandidates(
        project_id=project.id, run_id=run.id, model="test", marketplace="US", language="en",
        created_at=datetime.now(UTC), candidates=[candidate], warnings=[], rule_version="product-term-v1"
    )
    with repository.connect() as conn:
        conn.execute("INSERT INTO product_profiles VALUES(?,?,?,?)", (str(run.id), 1, "confirmed", profile.model_dump_json()))
        conn.execute("INSERT INTO root_candidates VALUES(?,?,?)", (str(run.id), "succeeded", roots_record.model_dump_json()))

    def model(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"items": [{
            "asin": "B000000002", "valid": True, "purpose": ["支撑大门"], "style_structure": ["夹具与轮子"],
            "specifications": ["户外"], "package_quantity": "1", "unit_basis": "each",
            "scores": {"purpose": 90, "style_structure": 90, "specifications": 90,
                       "purpose_reason": "用途相同", "style_structure_reason": "结构相同", "specifications_reason": "规格相同"}
        }]})}}]})

    qwen = QwenClient(QwenConfig(api_key="test"), transport=httpx.MockTransport(model))
    roots = RootService(repository, cast(ProductUnderstandingService, object()), qwen)
    profiles = ProductProfileService(repository, cast(object, object()), cast(object, object()), qwen)  # type: ignore[arg-type]
    service = ProductTermAnalysisService(
        repository, roots, profiles, cast(SearchProvider, OneProductSearch()), qwen
    )
    result = service.start(project.id, run.id)
    assert result.status == "completed"
    assert result.items[0].summary is not None
    assert result.items[0].summary.relevance_percent == Decimal("100")
    assert result.items[0].summary.confidence == "low"
    assert result.best_candidate_ids == [candidate.id]
    assert roots.get_selection(project.id, run.id).source == "automatic"


class KeywordSearch:
    """按关键词返回固定卡片；可模拟详情页被访问检查拦截或服务异常。"""

    def __init__(self, cards: dict[str, list[str]]) -> None:
        self.cards = cards
        self.blocked: set[str] = set()
        self.unavailable: set[str] = set()
        self.product_calls: list[str] = []

    def search(self, keyword: str, page_number: int, evidence_name: str) -> SearchPage:
        return SearchPage(
            region="New York 10001",
            cards=[
                SearchCard(asin=asin, position=position)
                for position, asin in enumerate(self.cards[keyword], 1)
            ],
            next_page=False,
            url=f"https://www.amazon.com/s?k={keyword}",
        )

    def product(self, asin: str) -> ProductSnapshot:
        self.product_calls.append(asin)
        if asin in self.blocked:
            raise SearchFailure("SEARCH_ACCESS_CHECK", "Amazon 要求人工确认访问，请打开检查窗口处理")
        if asin in self.unavailable:
            raise DomainError(502, "SEARCH_ACCESS_FAILED", "商品页返回 HTTP 503")
        return ProductSnapshot(asin=asin, title="Gate wheel", price=Decimal("20"), currency="USD")

    @contextmanager
    def open(
        self, marketplace: str, evidence_dir: Path
    ) -> Generator["KeywordSearch", None, None]:
        yield self


def comparison_model(*, valid: bool = True) -> QwenClient:
    def handle(request: httpx.Request) -> httpx.Response:
        prompt = json.loads(request.content)["messages"][0]["content"][0]["text"]
        data = cast(dict[str, object], json.loads(prompt.split("数据：", 1)[1]))
        products = cast(list[dict[str, str]], data["products"])
        items = [
            {
                "asin": product["asin"],
                "valid": valid,
                "exclusion_reason": "" if valid else "用途与本品的承重收纳用途不同",
                "purpose": ["支撑大门"] if valid else [],
                "style_structure": ["夹具与轮子"] if valid else [],
                "specifications": ["户外"] if valid else [],
                "package_quantity": "1" if valid else None,
                "unit_basis": "each" if valid else None,
                "scores": {
                    "purpose": 90,
                    "style_structure": 90,
                    "specifications": 90,
                    "purpose_reason": "用途相同",
                    "style_structure_reason": "结构相同",
                    "specifications_reason": "规格相同",
                }
                if valid
                else None,
            }
            for product in products
        ]
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"items": items})}}]
            },
        )

    return QwenClient(QwenConfig(api_key="test"), transport=httpx.MockTransport(handle))


def build_analysis(
    engine: Engine,
    tmp_path: Path,
    terms: list[str],
    search: KeywordSearch,
    client: QwenClient,
) -> tuple[UUID, UUID, list[RootCandidate], ProductTermAnalysisService, KeywordSearch]:
    repository = Repository(engine, tmp_path)
    repository.initialize()
    project = repository.create_project(ProjectCreate(name="商品词", marketplace="US"))
    run = repository.create_run(
        project.id, ProductInput(marketplace="US", asin="B000000001"), "analysis"
    )
    reference = ProductProfile(
        purpose=["支撑大门"], style_structure=["夹具与轮子"], specifications=["户外"],
        price=Decimal("20"), currency="USD", package_quantity=Decimal("1"), unit_basis="each",
        price_source="asin_current",
    )
    profile = ProductProfileRecord(
        project_id=project.id, run_id=run.id, version=1, status="confirmed", profile=reference
    )
    candidates = [
        RootCandidate(id=uuid4(), text=text, translation="大门配件", reason="同类商品")
        for text in terms
    ]
    roots_record = RootCandidates(
        project_id=project.id, run_id=run.id, model="test", marketplace="US", language="en",
        created_at=datetime.now(UTC), candidates=candidates, warnings=[], rule_version="product-term-v1"
    )
    with repository.connect() as conn:
        conn.execute("INSERT INTO product_profiles VALUES(?,?,?,?)", (str(run.id), 1, "confirmed", profile.model_dump_json()))
        conn.execute("INSERT INTO root_candidates VALUES(?,?,?)", (str(run.id), "succeeded", roots_record.model_dump_json()))
    roots = RootService(repository, cast(ProductUnderstandingService, object()), client)
    profiles = ProductProfileService(repository, cast(object, object()), cast(object, object()), client)  # type: ignore[arg-type]
    service = ProductTermAnalysisService(
        repository, roots, profiles, cast(SearchProvider, search), client
    )
    return project.id, run.id, candidates, service, search


def test_completed_analysis_is_reused_without_recollection(engine: Engine, tmp_path: Path) -> None:
    search = KeywordSearch({"gate wheel": ["B000000010"]})
    project, run, candidates, service, search = build_analysis(
        engine, tmp_path, ["gate wheel"], search, comparison_model()
    )
    first = service.start(project, run)
    assert first.status == "completed"
    assert first.best_candidate_ids == [candidates[0].id]
    calls = list(search.product_calls)
    second = service.start(project, run)
    assert second.id == first.id
    assert search.product_calls == calls


def test_access_check_pauses_and_resumes_only_unfinished_terms(
    engine: Engine, tmp_path: Path
) -> None:
    search = KeywordSearch({"gate wheel": ["B000000010"], "gate hinge": ["B000000020"]})
    project, run, _candidates, service, search = build_analysis(
        engine, tmp_path, ["gate wheel", "gate hinge"], search, comparison_model()
    )
    search.blocked.add("B000000020")

    with pytest.raises(DomainError) as error:
        service.start(project, run)
    assert error.value.code == "SEARCH_ACCESS_CHECK"
    saved = service.get(project, run)
    assert saved.status == "incomplete"
    assert saved.complete is False
    assert [item.status for item in saved.items] == ["completed", "failed"]
    assert "人工确认访问" in saved.items[1].error

    search.blocked.clear()
    resumed = service.start(project, run)
    assert resumed.status == "completed"
    assert [item.status for item in resumed.items] == ["completed", "completed"]
    # 已完成的词条只采一次；被拦截的词条在恢复后重采。
    assert search.product_calls == ["B000000010", "B000000020", "B000000020"]


def test_zero_collected_products_is_incomplete_and_retried(
    engine: Engine, tmp_path: Path
) -> None:
    search = KeywordSearch({"gate wheel": ["B000000010"]})
    project, run, _candidates, service, search = build_analysis(
        engine, tmp_path, ["gate wheel"], search, comparison_model()
    )
    search.unavailable.add("B000000010")

    result = service.start(project, run)
    assert result.status == "incomplete"
    assert result.complete is False
    assert result.best_candidate_ids == []
    assert result.items[0].status == "failed"
    assert "未取得任何商品详情" in result.items[0].error

    search.unavailable.clear()
    retried = service.start(project, run)
    assert retried.status == "completed"
    assert retried.items[0].status == "completed"
    assert len(search.product_calls) == 2


def test_unrankable_complete_analysis_waits_for_human_without_paid_retry(
    engine: Engine, tmp_path: Path
) -> None:
    search = KeywordSearch({"gate wheel": ["B000000010"]})
    project, run, _candidates, service, search = build_analysis(
        engine, tmp_path, ["gate wheel"], search, comparison_model(valid=False)
    )
    result = service.start(project, run)
    assert result.complete is True
    assert result.status == "awaiting_review"
    assert result.best_candidate_ids == []
    # 全部剔除是真实结论，不应在每次重跑时重复调用模型和浏览器。
    calls = list(search.product_calls)
    assert service.start(project, run).id == result.id
    assert search.product_calls == calls


def test_refresh_recollects_a_completed_analysis(engine: Engine, tmp_path: Path) -> None:
    search = KeywordSearch({"gate wheel": ["B000000010"]})
    project, run, _candidates, service, search = build_analysis(
        engine, tmp_path, ["gate wheel"], search, comparison_model()
    )
    first = service.start(project, run)
    assert len(search.product_calls) == 1
    refreshed = service.start(project, run, refresh=True)
    assert refreshed.id != first.id
    assert refreshed.status == "completed"
    assert len(search.product_calls) == 2
    assert service.get(project, run, first.id) == first
    assert service.start(project, run).id == refreshed.id
    assert len(service.history(project, run)) == 2
