"""Persisted analysis runs real candidate, model-validation, collection and scoring code."""

import json
from collections.abc import Generator, Sequence
from contextlib import AbstractContextManager, contextmanager
from decimal import Decimal
from pathlib import Path
from typing import cast
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from test_aba import auth_file, row
from test_hot_search_candidates import seed_product_terms

from app.adapters.aba import AbaClient
from app.db.repository import Repository
from app.domain.aba import AbaReport, AbaRequest
from app.domain.asin_product import ProductSnapshot
from app.domain.product_terms import ProductProfile, ProductProfileRecord
from app.domain.verification import SearchCard, SearchPage
from app.main import create_app
from app.services.aba import AbaService
from app.services.core_terms import CoreService
from app.services.hot_search_core_analysis import (
    HotSearchCoreAnalysis,
    HotSearchCoreAnalysisService,
)
from app.services.product_profiles import ProductProfileService
from app.services.qwen import QwenClient, QwenConfig


class Search:
    def __init__(
        self,
        repository: Repository,
        count: int = 35,
        failing: Sequence[str] = (),
        failed_details: bool = False,
    ) -> None:
        self.repository = repository
        self.count = count
        self.failing = failing
        self.seen: set[str] = set()
        self.failed_details = failed_details

    @contextmanager
    def open(self, marketplace: str, evidence_dir: Path) -> Generator["Search", None, None]:
        yield self

    def search(self, keyword: str, page_number: int, evidence_name: str) -> SearchPage:
        assert_call_started(self.repository, "search")
        if keyword in self.failing:
            raise RuntimeError("search unavailable")
        return SearchPage(
            region="New York 10001",
            next_page=False,
            cards=[SearchCard(asin="B000000001", position=1)]
            + [SearchCard(asin=f"B{i:09d}", position=i + 1) for i in range(1, self.count + 1)]
            if self.count
            else [],
        )

    def product(self, asin: str) -> ProductSnapshot:
        assert_call_started(self.repository, "product")
        # Any duplicate read is a real resource-usage regression, rather than a mock count.
        if asin in self.seen:
            raise RuntimeError("duplicate detail request")
        self.seen.add(asin)
        if self.failed_details:
            raise RuntimeError("detail outcome unknown")
        return ProductSnapshot(
            asin=asin,
            title="shoe rack",
            price=Decimal("20"),
            currency="USD",
            price_reliable=int(asin[1:]) <= 25,
        )


def assert_call_started(repository: Repository, kind: str) -> None:
    with repository.connect() as conn:
        document = json.loads(
            conn.execute(
                "SELECT document FROM hot_search_core_analyses ORDER BY seq DESC LIMIT 1"
            ).fetchone()[0]
        )
    assert any(
        batch["kind"] == kind and batch["status"] == "started" for batch in document["batches"]
    )


def setup_analysis(
    db: str,
    engine: Engine,
    tmp_path: Path,
    terms: list[str] | None = None,
    count: int = 35,
    failing: Sequence[str] = (),
    model_mode: str = "normal",
) -> tuple[HotSearchCoreAnalysisService, UUID, UUID, AbaReport]:
    terms = terms or ["shoe storage"]
    repository = Repository(engine, tmp_path)
    aba_client = AbaClient(
        auth_file(tmp_path),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "reportsV2": [
                        {
                            "totalItems": len(terms),
                            "rows": [row(term, index + 1) for index, term in enumerate(terms)],
                        }
                    ]
                },
            )
        ),
    )
    with TestClient(create_app(db, storage_root=tmp_path, aba_client=aba_client)) as client:
        project, run = seed_product_terms(client, engine, tmp_path)
    project, run = UUID(project), UUID(run)
    reference = ProductProfile(
        purpose=["store shoes"],
        style_structure=["rack"],
        specifications=["indoor"],
        price=Decimal("20"),
        currency="USD",
        package_quantity=Decimal("1"),
        unit_basis="each",
    )
    profile = ProductProfileRecord(
        project_id=project,
        run_id=run,
        version=1,
        status="confirmed",
        profile=reference,
    )
    with repository.connect() as conn:
        conn.execute(
            "INSERT INTO product_profiles VALUES(?,?,?,?)",
            (str(run), 1, "confirmed", profile.model_dump_json()),
        )
    aba = AbaService(repository, aba_client)
    report = aba.collect(project, run, AbaRequest(query_mode="product_term"))
    comparison_number = 0
    invalid_remaining = 1 if model_mode == "invalid_once" else 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal comparison_number, invalid_remaining
        prompt = json.loads(request.content)["messages"][-1]["content"][0]["text"]
        payload = json.loads(prompt.rsplit("数据：", 1)[1])
        kind = "intent" if "terms" in payload else "comparison"
        assert_call_started(repository, kind)
        if invalid_remaining:
            invalid_remaining -= 1
            content = "{}"
        elif kind == "intent":
            gates = ("object_match", "purpose_match", "product_level_match", "modifiers_compatible")
            content = json.dumps(
                {
                    "items": [
                        {
                            "candidate_id": term["candidate_id"],
                            "brand_category": "competitor_brand"
                            if term["keyword"] == "other brand"
                            else "generic",
                            "intent_evidence": {
                                **dict.fromkeys(gates, True),
                                "category": "exact",
                                "reasons": dict.fromkeys(gates, "Confirmed shoe rack"),
                                "explicit_conflict": False,
                                "accessory_target": False,
                                "broad_category": False,
                                **(
                                    {
                                        "object_match": False,
                                        "broad_category": True,
                                        "category": "broad",
                                    }
                                    if term["keyword"] == "furniture"
                                    else {}
                                ),
                            },
                        }
                        for term in payload["terms"]
                    ]
                }
            )
        else:
            comparison_number += 1
            if model_mode == "crash_second" and comparison_number == 2:
                raise SystemExit("simulated process termination")
            if model_mode == "timeout":
                raise httpx.ReadTimeout("unknown charged outcome", request=request)
            content = json.dumps(
                {
                    "items": [
                        {
                            "asin": product["asin"],
                            "valid": True,
                            "exclusion_reason": "",
                            "purpose": ["store shoes"],
                            "style_structure": ["rack"],
                            "specifications": ["indoor"],
                            "package_quantity": "1",
                            "unit_basis": "each",
                            "scores": {
                                **dict.fromkeys(
                                    ("purpose", "style_structure", "specifications"),
                                    90
                                    if int(product["asin"][1:]) <= 16
                                    and (model_mode != "independent" or comparison_number == 1)
                                    else 20,
                                ),
                                **dict.fromkeys(
                                    (
                                        "purpose_reason",
                                        "style_structure_reason",
                                        "specifications_reason",
                                    ),
                                    "Compared against confirmed profile",
                                ),
                            },
                        }
                        for product in payload["products"]
                    ]
                }
            )
        return httpx.Response(
            200, json={"choices": [{"finish_reason": "stop", "message": {"content": content}}]}
        )

    model = QwenClient(QwenConfig(api_key="test"), transport=httpx.MockTransport(handle))
    profiles = ProductProfileService(repository, None, None, model)  # type: ignore[arg-type]
    service = HotSearchCoreAnalysisService(
        repository,
        CoreService(repository, aba, model),
        profiles,
        Search(repository, count, failing),
        model,
    )
    return service, project, run, report


def execute(
    service: HotSearchCoreAnalysisService,
    project: UUID,
    run: UUID,
    report: AbaReport,
    own_brands: list[str] | None = None,
) -> HotSearchCoreAnalysis:
    queued = service.start(project, run, report.id, own_brands or [])
    assert queued.status == "queued"
    return service.execute(project, run, queued.id)


def test_analyzes_thirty_unique_products_with_three_dimension_fallback(
    db: str, engine: Engine, tmp_path: Path
) -> None:
    # 价格不可比仍保留用途、结构和规格有效的样本，并按完整有效分母计算。
    service, project, run, report = setup_analysis(db, engine, tmp_path)
    result = execute(service, project, run, report)
    item = result.items[0]
    assert result.status == "completed"
    assert len(item.comparisons) == 30
    assert item.summary is not None
    assert item.summary.valid_samples == 30
    assert item.summary.high_related_count == 16
    assert item.summary.relevance_percent == Decimal("53.33333333333333333333333333")
    assert item.comparisons[0].overall_score == Decimal("91")
    assert all(c.price_score is None and not c.exclusion_reason for c in item.comparisons[25:])
    assert len(result.snapshots) == 30
    assert len(result.collections[str(item.candidate_id)].unique_asins) == 30
    assert result.recommended_candidate_ids == [item.candidate_id]
    assert all(batch.status == "completed" for batch in result.batches)


def test_brand_and_intent_gate_even_when_relevance_is_high(db: str, engine: Engine, tmp_path: Path) -> None:
    service, project, run, report = setup_analysis(
        db,
        engine,
        tmp_path,
        ["shoe storage", "furniture", "other brand", "MyBrand rack"],
        count=1,
    )
    result = execute(service, project, run, report, own_brands=["mybrand"])
    assert [
        item.keyword
        for item in result.items
        if item.candidate_id in result.recommended_candidate_ids
    ] == ["shoe storage"]
    assert all(
        item.summary is not None and item.summary.relevance_percent == 100 for item in result.items
    )
    assert {item.keyword: item.brand_category for item in result.items}[
        "MyBrand rack"
    ] == "own_brand"


def test_all_forty_eligible_candidates_are_recommended_in_stable_order(db: str, engine: Engine, tmp_path: Path) -> None:
    terms = [f"storage {index:02}" for index in range(40)]
    service, project, run, report = setup_analysis(db, engine, tmp_path, terms, count=1)
    result = execute(service, project, run, report)
    assert len(result.items) == len(result.recommended_candidate_ids) == 40
    assert [item.keyword for item in result.items] == terms
    assert result.recommended_candidate_ids == [item.candidate_id for item in result.items]


def test_one_failed_word_does_not_prevent_following_word(db: str, engine: Engine, tmp_path: Path) -> None:
    service, project, run, report = setup_analysis(
        db,
        engine,
        tmp_path,
        ["bad search", "shoe storage"],
        count=1,
        failing=["bad search"],
    )
    result = execute(service, project, run, report)
    assert result.status == "incomplete"
    assert {item.keyword: item.status for item in result.items} == {
        "bad search": "failed",
        "shoe storage": "completed",
    }
    assert len(result.recommended_candidate_ids) == 1
    failed = next(item for item in result.items if item.status == "failed")
    assert "search unavailable" in " ".join(failed.exclusion_reasons)


def test_no_samples_remain_unknown_and_cannot_qualify(db: str, engine: Engine, tmp_path: Path) -> None:
    service, project, run, report = setup_analysis(db, engine, tmp_path, count=0)
    result = execute(service, project, run, report)
    assert result.items[0].status == "no_samples"
    assert result.items[0].summary is not None
    assert result.items[0].summary.relevance_percent is None
    assert result.recommended_candidate_ids == []
    assert result.status == "completed"
    assert result.complete is True


def test_shared_details_are_reused_but_each_term_is_scored_independently(db: str, engine: Engine, tmp_path: Path) -> None:
    service, project, run, report = setup_analysis(
        db,
        engine,
        tmp_path,
        ["shoe storage", "shoe organizer"],
        count=1,
        model_mode="independent",
    )
    result = execute(service, project, run, report)
    assert len(result.snapshots) == 1
    assert {
        item.keyword: item.summary.relevance_percent
        for item in result.items
        if item.summary is not None
    } == {
        "shoe storage": Decimal("100"),
        "shoe organizer": Decimal("0"),
    }
    assert all(item.status == "completed" for item in result.items)


def test_explicit_invalid_output_gets_a_persisted_limited_retry(db: str, engine: Engine, tmp_path: Path) -> None:
    service, project, run, report = setup_analysis(
        db, engine, tmp_path, count=1, model_mode="invalid_once"
    )
    result = execute(service, project, run, report)
    assert result.status == "completed"
    intent = [batch for batch in result.batches if batch.kind == "intent"]
    assert [batch.status for batch in intent] == ["invalid", "completed"]
    assert intent[0].error_code == "MODEL_OUTPUT_INVALID"


def test_unknown_model_outcome_is_never_automatically_resent(db: str, engine: Engine, tmp_path: Path) -> None:
    service, project, run, report = setup_analysis(
        db, engine, tmp_path, count=1, model_mode="timeout"
    )
    result = execute(service, project, run, report)
    assert result.status == "failed"
    comparisons = [batch for batch in result.batches if batch.kind == "comparison"]
    assert len(comparisons) == 1
    assert comparisons[0].status == "outcome_unknown"
    assert service.execute(project, run, result.id).status == "failed"
    assert len(service.get(project, run).batches) == len(result.batches)


def test_failed_detail_is_excluded_without_redispatch_across_terms(db: str, engine: Engine, tmp_path: Path) -> None:
    # Catches treating a detail timeout as permission to resend when another term has the same ASIN.
    service, project, run, report = setup_analysis(
        db, engine, tmp_path, ["shoe storage", "shoe organizer"], count=1
    )
    cast(Search, service.search).failed_details = True
    result = execute(service, project, run, report)
    assert all(item.status == "no_samples" for item in result.items)
    assert all(item.comparisons[0].exclusion_reason for item in result.items)
    assert result.recommended_candidate_ids == []
    assert len([batch for batch in result.batches if batch.kind == "product"]) == 1
    assert result.status == "incomplete"
    assert result.complete is False


def test_partial_detail_failure_preserves_eligible_word_and_savable_subset(
    db: str, engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, project, run, report = setup_analysis(db, engine, tmp_path, count=2)
    search = cast(Search, service.search)
    original = search.product

    def product(asin: str) -> ProductSnapshot:
        if asin == "B000000002":
            assert_call_started(service.repository, "product")
            raise RuntimeError("detail response lost")
        return original(asin)

    monkeypatch.setattr(search, "product", product)
    result = execute(service, project, run, report)
    assert result.status == "incomplete"
    assert result.complete is False
    assert result.items[0].status == "completed"
    assert result.items[0].summary is not None
    assert result.items[0].summary.valid_samples == 1
    assert result.recommended_candidate_ids == [result.items[0].candidate_id]
    with TestClient(create_app(db, storage_root=tmp_path)) as api:
        response = api.put(
            f"/api/v1/projects/{project}/pipeline-runs/{run}/core-selection",
            json={
                "analysis_id": str(result.id),
                "rule_version": "hot-search-core-v1",
                "candidate_ids": [str(result.items[0].candidate_id)],
                "expected_version": 0,
            },
        )
        assert response.status_code == 200, response.text


def test_all_three_invalid_schema_attempts_are_logged_and_then_stop(db: str, engine: Engine, tmp_path: Path) -> None:
    service, project, run, report = setup_analysis(db, engine, tmp_path, count=1)
    assert service.client is not None
    service.client.transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]},
        )
    )
    result = execute(service, project, run, report)
    assert result.status == "failed"
    assert [batch.status for batch in result.batches] == ["invalid", "invalid", "invalid"]


def test_provider_open_is_claimed_before_invoking_external_provider(
    db: str, engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, project, run, report = setup_analysis(db, engine, tmp_path, count=1)
    search = cast(Search, service.search)
    original_open = search.open

    def eager_open(marketplace: str, evidence_dir: Path) -> AbstractContextManager[Search]:
        assert_call_started(service.repository, "open")
        return original_open(marketplace, evidence_dir)

    monkeypatch.setattr(search, "open", eager_open)
    result = execute(service, project, run, report)
    assert result.status == "completed"
