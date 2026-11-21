"""第三步复用商品快照及 30 天均价预筛的专项回归。"""

from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import Engine
from test_competitors import ready

from app.adapters.poe_downloads import PoeArchive, PoeBrowserDownloader
from app.db.repository import Repository
from app.domain.asin_product import ProductSnapshot
from app.domain.errors import DomainError
from app.domain.product_terms import (
    BatchComparisonDraft,
    BatchComparisonSet,
    DimensionScores,
    ProductComparison,
    ProductProfile,
    ProductProfileRecord,
    ProductTermAnalysis,
    TermAnalysisItem,
    TermSummary,
)
from app.domain.verification import CollectionResult, SearchPage
from app.main import create_app
from app.services.amazon_search import SearchSession, parse_snapshot
from app.services.competitors import CompetitorService
from app.services.core_terms import CoreService
from app.services.hot_search_core_analysis import HotSearchCoreAnalysis
from app.services.asin_product import ProductProvider
from app.services.qwen import QwenClient
from app.workflows.deps import WorkflowDeps


def test_automatic_method_three_rejects_non_30_day_price_column() -> None:
    from app.adapters.competitor_csv import parse_competitor_csv

    with pytest.raises(DomainError) as error:
        parse_competitor_csv(
            "ASIN,Average Selling Price (Past 90 Days) (USD)\nB000000001,100.00\n",
            "wrong-period.csv",
            require_30_day=True,
        )
    assert error.value.code == "COMPETITOR_CSV_30_DAY_REQUIRED"


def _ids(base: str) -> tuple[UUID, UUID]:
    return UUID(base.split("/")[4]), UUID(base.split("/")[6])


def _profile(engine: Engine, tmp_path: Path, base: str) -> ProductProfile:
    project, run = _ids(base)
    profile = ProductProfile(
        asin="B0G3GRFJYB",
        purpose=["哑铃收纳"],
        style_structure=["落地支架"],
        specifications=["可调节"],
        package_quantity=Decimal("1"),
        unit_basis="each",
        price=Decimal("100"),
        currency="USD",
        price_source="user_reference",
    )
    record = ProductProfileRecord(
        project_id=project,
        run_id=run,
        version=1,
        status="confirmed",
        profile=profile,
    )
    with Repository(engine, tmp_path).connect() as conn:
        conn.execute(
            "INSERT INTO product_profiles VALUES(?,?,?,?)",
            (str(run), 1, "confirmed", record.model_dump_json()),
        )
    return profile


def _step_one_analysis(
    engine: Engine,
    tmp_path: Path,
    base: str,
    snapshots: list[ProductSnapshot],
    scores: dict[str, Decimal] | None = None,
) -> None:
    project, run = _ids(base)
    scores = scores or {}
    comparisons = [
        ProductComparison(
            asin=asin,
            status="high_related",
            purpose_score=score,
            style_structure_score=score,
            specifications_score=score,
            overall_score=score,
            high_related=True,
        )
        for asin, score in scores.items()
    ]
    analysis = ProductTermAnalysis(
        id=uuid4(),
        project_id=project,
        run_id=run,
        profile_version=1,
        status="completed",
        complete=True,
        items=[
            TermAnalysisItem(
                candidate_id=uuid4(),
                text="product term",
                status="completed",
                summary=TermSummary(
                    valid_samples=len(comparisons),
                    high_related_count=len(comparisons),
                    relevance_percent=Decimal("100") if comparisons else None,
                    confidence="medium" if comparisons else "none",
                ),
                comparisons=comparisons,
                collection=CollectionResult(
                    term="product term",
                    raw_natural_count=len(snapshots),
                    unique_asins=[item.asin for item in snapshots],
                    products=snapshots,
                ),
            )
        ],
    )
    with Repository(engine, tmp_path).connect() as conn:
        conn.execute(
            "INSERT INTO product_term_analyses (id,run_id,fingerprint,document) VALUES(?,?,?,?)",
            (str(analysis.id), str(run), str(uuid4()), analysis.model_dump_json()),
        )


def _step_two_analysis(
    engine: Engine,
    tmp_path: Path,
    base: str,
    profile: ProductProfile,
    snapshots: list[ProductSnapshot],
) -> None:
    project, run = _ids(base)
    analysis = HotSearchCoreAnalysis(
        project_id=project,
        run_id=run,
        report_id=uuid4(),
        fingerprint=str(uuid4()),
        profile_version=1,
        profile_snapshot=profile,
        own_brands_snapshot=[],
        marketplace="US",
        delivery_region="US 10001",
        input_snapshot={},
        status="completed",
        complete=True,
        items=[],
        snapshots={item.asin: item for item in snapshots},
    )
    with Repository(engine, tmp_path).connect() as conn:
        conn.execute(
            "INSERT INTO hot_search_core_analyses (id,run_id,fingerprint,document) VALUES(?,?,?,?)",
            (str(analysis.id), str(run), analysis.fingerprint, analysis.model_dump_json()),
        )


def _comparison(products: list[ProductSnapshot], score: int = 80) -> BatchComparisonSet:
    return BatchComparisonSet(
        items=[
            BatchComparisonDraft(
                asin=item.asin,
                purpose=["哑铃收纳"],
                style_structure=["落地支架"],
                specifications=["可调节"],
                scores=DimensionScores(
                    purpose=score,
                    style_structure=score,
                    specifications=score,
                    purpose_reason="用途",
                    style_structure_reason="结构",
                    specifications_reason="规格",
                ),
            )
            for item in products
        ]
    )


def test_shared_cache_reads_both_steps_prefers_newest_and_expires_old(
    db: str, engine: Engine, tmp_path: Path
) -> None:
    client, base = ready(db, engine, tmp_path)
    client.close()
    now = datetime.now(UTC)
    profile = _profile(engine, tmp_path, base)
    _step_one_analysis(
        engine,
        tmp_path,
        base,
        [
            ProductSnapshot(asin="B000000001", title="step one", captured_at=now - timedelta(hours=2)),
            ProductSnapshot(asin="B000000009", title="expired", captured_at=now - timedelta(hours=25)),
        ],
    )
    _step_two_analysis(
        engine,
        tmp_path,
        base,
        profile,
        [
            ProductSnapshot(asin="B000000001", title="step two", captured_at=now - timedelta(hours=1)),
            ProductSnapshot(asin="B000000002", title="step two only", captured_at=now - timedelta(hours=1)),
        ],
    )
    _, run = _ids(base)
    service = CompetitorService(
        Repository(engine, tmp_path),
        cast(CoreService, None),
        cast(ProductProvider, None),
    )
    service._cache_product_snapshot(
        run,
        ProductSnapshot(asin="B000000003", title="step three persisted", captured_at=now),
    )

    cached = service._shared_product_snapshots(run, now=now)

    assert set(cached) == {"B000000001", "B000000002", "B000000003"}
    assert cached["B000000001"].title == "step two"
    assert cached["B000000003"].title == "step three persisted"


def test_method_two_reuses_cached_details_and_scores_only_new_asins(
    db: str, engine: Engine, tmp_path: Path
) -> None:
    client, base = ready(db, engine, tmp_path)
    _profile(engine, tmp_path, base)
    _step_one_analysis(
        engine,
        tmp_path,
        base,
        [ProductSnapshot(asin="B000000001", title="cached title")],
        {"B000000001": Decimal("80")},
    )
    manual = client.post(
        base + "/competitors/discover",
        json={
            "source": "manual",
            "asins": ["B000000001", "B000000002"],
            "expected_version": 0,
        },
    )
    assert manual.status_code == 200, manual.text
    client.close()
    fetched: list[str] = []
    compared: list[list[str]] = []

    class Products:
        def fetch(self, marketplace: str, asin: str) -> ProductSnapshot:
            assert marketplace == "US"
            fetched.append(asin)
            if asin == "B000000003":
                return ProductSnapshot(asin="B999999999", title="wrong identity")
            return ProductSnapshot(asin=asin, title="new title")

    class Search:
        def search(self, keyword: str, page_number: int, evidence_name: str) -> SearchPage:
            return parse_snapshot(
                {
                    "region": "US 10001",
                    "ready": True,
                    "next_page": False,
                    "pagination_known": True,
                    "cards": [
                        {"asin": "B000000001", "sponsored": False},
                        {"asin": "B000000002", "sponsored": False},
                        {"asin": "B000000003", "sponsored": False},
                    ],
                }
            )

        @contextmanager
        def open(self, marketplace: str, evidence_dir: Path) -> Generator[SearchSession]:
            yield cast(SearchSession, self)

    class Config:
        def validate(self) -> None:
            return None

    class Model:
        config = Config()

        def compare_product_batch(
            self, reference: ProductProfile, products: list[ProductSnapshot]
        ) -> BatchComparisonSet:
            compared.append([item.asin for item in products])
            return _comparison(products, 70)

    with TestClient(
        create_app(
            db,
            storage_root=tmp_path,
            product_provider=Products(),
            search_provider=Search(),
            model_client=cast(QwenClient, Model()),
        )
    ) as api:
        response = api.post(
            base + "/competitors/discover",
                json={"source": "front_search", "expected_version": 1},
        )
    assert response.status_code == 200, response.text
    by_asin = {item["asin"]: item for item in response.json()["items"]}
    assert fetched == ["B000000002", "B000000003"]
    assert compared == [["B000000002"]]
    assert by_asin["B000000001"]["title"] == "cached title"
    assert by_asin["B000000001"]["relevance_score"] == "80"
    assert by_asin["B000000002"]["title"] == "new title"
    assert by_asin["B000000002"]["relevance_score"] == "70"
    assert by_asin["B000000003"]["title"] == ""
    assert by_asin["B000000003"]["relevance_score"] is None
    _, run = _ids(base)
    cache = tmp_path / "competitor_product_snapshots" / str(run)
    assert not (cache / "B999999999.json").exists()


def test_method_three_filters_30_day_price_before_details_and_preserves_average(
    db: str, engine: Engine, tmp_path: Path, monkeypatch
) -> None:
    client, base = ready(db, engine, tmp_path)
    profile = _profile(engine, tmp_path, base)
    _step_two_analysis(
        engine,
        tmp_path,
        base,
        profile,
        [ProductSnapshot(asin="B000000001", title="cached", price=Decimal("500"), currency="USD")],
    )
    fetched: list[str] = []
    compared: list[list[str]] = []

    class Products:
        def fetch(self, marketplace: str, asin: str) -> ProductSnapshot:
            fetched.append(asin)
            return ProductSnapshot(
                asin=asin,
                title=f"detail {asin}",
                price=Decimal("999"),
                currency="USD",
            )

    class Config:
        def validate(self) -> None:
            return None

    class Model:
        config = Config()

        def compare_product_batch(
            self, reference: ProductProfile, products: list[ProductSnapshot]
        ) -> BatchComparisonSet:
            compared.append([item.asin for item in products])
            return _comparison(products, 80)

    def download(self: PoeBrowserDownloader, run_id: UUID, keyword: str) -> PoeArchive:
        source = tmp_path / "products.csv"
        source.write_text(
            "ASIN,Product Name,Average Selling Price (Past 30 Days),Currency\n"
            "B000000001,Cached,85.00,USD\n"
            "B000000002,Missing,100.00,USD\n"
            "B000000003,Outside,84.99,USD\n"
            "B000000004,Wrong currency,100.00,EUR\n",
            encoding="utf-8",
        )
        return self.store.archive(
            source,
            run_id,
            keyword,
            "Products.csv",
            "https://sellercentral.amazon.com/opportunity-explorer/explore",
        )

    monkeypatch.setattr(PoeBrowserDownloader, "download", download)
    deps = cast(WorkflowDeps, cast(FastAPI, client.app).state.workflow_service.deps)
    deps.competitors.poe_downloader = PoeBrowserDownloader(tmp_path / "aba", deps.competitors.poe_store)
    deps.competitors.products = cast(ProductProvider, Products())
    deps.competitors.client = cast(QwenClient, Model())
    try:
        response = client.post(
            base + "/competitors/poe-download-all",
            json={"expected_version": 0},
        )
    finally:
        client.close()
    assert response.status_code == 200, response.text
    result = response.json()
    assert fetched == ["B000000002"]
    assert compared == [["B000000001", "B000000002"]]
    assert [item["asin"] for item in result["items"]] == ["B000000001", "B000000002"]
    assert [item["price"] for item in result["items"]] == ["85.00", "100.00"]
    assert all(item["price_basis"] == "average_30d" for item in result["items"])
    assert all(item["price_status"] == "within" for item in result["items"])
