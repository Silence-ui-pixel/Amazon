"""第三步真实接口回归：版本、价格未知、来源和恢复不得被误报为完成。"""

import json
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import Engine
from test_core_terms import setup_case

from app.db.repository import Repository
from app.domain.product_terms import (
    ProductComparison,
    ProductTermAnalysis,
    TermAnalysisItem,
    TermSummary,
)
from app.main import create_app


def ready(db: str, engine: Engine, tmp_path: Path, mode: str = "ok") -> tuple[TestClient, str]:
    client, base, report, _ = setup_case(db, engine, tmp_path, mode)
    analysis = client.post(base + f"/aba-reports/{report}/core-analyses", json={}).json()
    terms = [x["id"] for x in analysis["items"] if x["recommended"]]
    response = client.put(
        base + "/core-selection",
        json={"analysis_id": analysis["id"], "candidate_ids": terms, "expected_version": 0},
    )
    assert response.status_code == 200, response.text
    return client, base


def replace_core_keywords(engine: Engine, tmp_path: Path, keywords: list[str]) -> None:
    with Repository(engine, tmp_path).connect() as conn:
        row = conn.execute(
            "SELECT id,document FROM core_selections ORDER BY version DESC LIMIT 1"
        ).fetchone()
        assert row is not None
        document = json.loads(row[1])
        template = document["items"][0]
        document["items"] = []
        for index, keyword in enumerate(keywords, start=1):
            item = deepcopy(template)
            item["id"] = str(uuid4())
            item["keyword"] = keyword
            item["search_frequency_rank"] = index
            document["items"].append(item)
        conn.execute(
            "UPDATE core_selections SET document=? WHERE id=?",
            (json.dumps(document), row[0]),
        )


def save_product_term_scores(
    engine: Engine, tmp_path: Path, run_id: str, scores: list[tuple[str, str]]
) -> None:
    with Repository(engine, tmp_path).connect() as conn:
        project_id = conn.execute(
            "SELECT project_id FROM pipeline_runs WHERE id=?", (run_id,)
        ).fetchone()[0]
        comparisons = [
            ProductComparison(
                asin=asin,
                status="high_related",
                purpose_score=80,
                style_structure_score=80,
                specifications_score=80,
                price_score=Decimal(score),
                overall_score=Decimal(score),
                high_related=True,
            )
            for asin, score in scores
        ]
        analysis = ProductTermAnalysis(
            id=uuid4(),
            project_id=project_id,
            run_id=run_id,
            profile_version=1,
            status="completed",
            complete=True,
            items=[
                TermAnalysisItem(
                    candidate_id=uuid4(),
                    text="four dimension term",
                    status="completed",
                    summary=TermSummary(
                        valid_samples=len(comparisons),
                        high_related_count=len(comparisons),
                        relevance_percent=Decimal("100"),
                        confidence="medium",
                    ),
                    comparisons=comparisons,
                )
            ],
            created_at=datetime.now(UTC),
        )
        conn.execute(
            "INSERT INTO product_term_analyses VALUES(?,?,?,?)",
            (str(analysis.id), run_id, str(uuid4()), analysis.model_dump_json()),
        )


def test_manual_review_price_override_versions_and_restart(db: str, engine: Engine, tmp_path: Path) -> None:
    client, base = ready(db, engine, tmp_path)
    url = base + "/competitors"
    try:
        response = client.post(
            url + "/discover",
            json={
                "source": "manual",
                "asins": ["b012345678", "B012345678", "B087654321"],
                "expected_version": 0,
            },
        )
        assert response.status_code == 200, response.text
        workspace = response.json()
        assert [x["asin"] for x in workspace["items"]] == ["B012345678", "B087654321"]
        assert workspace["confirmed_at"] is None
        reviews = [
            {
                "asin": "B012345678",
                "decision": "include",
                "reason": "图片与商品功能匹配",
                "price_override_reason": "",
            },
            {
                "asin": "B087654321",
                "decision": "exclude",
                "reason": "产品不相关",
                "price_override_reason": "",
            },
        ]
        payload = {
            "expected_version": 1,
            "reference_price": "20.00",
            "currency": "USD",
            "tolerance": "0.15",
            "reviews": reviews,
        }
        assert client.put(url + "/selection", json=payload).status_code == 422
        reviews[0]["price_override_reason"] = "已线下核对价格，保留待后续补充"
        response = client.put(url + "/selection", json=payload)
        assert response.status_code == 200, response.text
        saved = response.json()
        assert saved["items"][0]["price_status"] == "unknown"
        assert saved["confirmed_at"] and saved["version"] == 2
        assert client.put(url + "/selection", json=payload).json() == saved
        assert (
            client.post(
                url + "/discover",
                json={"source": "manual", "asins": ["B000000003"], "expected_version": 1},
            ).status_code
            == 409
        )
        foreign = base.replace(base.split("/")[4], str(uuid4()))
        assert client.get(foreign + "/competitors").status_code == 404
    finally:
        client.close()
    with TestClient(create_app(db, storage_root=tmp_path)) as restarted:
        assert restarted.get(url).json() == saved


def test_step_two_required_and_changed_selection_marks_stale(db: str, engine: Engine, tmp_path: Path) -> None:
    client, base, report, _ = setup_case(db, engine, tmp_path)
    try:
        url = base + "/competitors"
        assert (
            client.post(
                url + "/discover",
                json={"source": "manual", "asins": ["B012345678"], "expected_version": 0},
            ).status_code
            == 409
        )
        analysis = client.post(base + f"/aba-reports/{report}/core-analyses", json={}).json()
        request = {
            "analysis_id": analysis["id"],
            "candidate_ids": [next(x["id"] for x in analysis["items"] if x["recommended"])],
            "expected_version": 0,
        }
        assert client.put(base + "/core-selection", json=request).status_code == 200
        assert (
            client.post(
                url + "/discover",
                json={"source": "manual", "asins": ["B012345678"], "expected_version": 0},
            ).status_code
            == 200
        )
        analysis2 = client.post(
            base + f"/aba-reports/{report}/core-analyses", json={"own_brands": ["Acme"]}
        ).json()
        request.update(
            analysis_id=analysis2["id"],
            candidate_ids=[next(x["id"] for x in analysis2["items"] if x["recommended"])],
            expected_version=1,
        )
        assert client.put(base + "/core-selection", json=request).status_code == 200
        assert client.get(url).json()["stale"] is True
        assert (
            client.put(url + "/selection", json={"expected_version": 1, "reviews": []}).status_code
            == 409
        )
    finally:
        client.close()


def test_poe_csv_prices_provenance_and_invalid_atomic_import(db: str, engine: Engine, tmp_path: Path) -> None:
    client, base = ready(db, engine, tmp_path)
    url = base + "/competitors"
    try:
        csv = "ASIN,Product Name,Average Selling Price (90 days),Currency,Image URL\nB012345678,Cooling blanket,23.00,USD,https://example.com/product.jpg\nB087654321,Other,23.01,USD,\nB000000003,Foreign,20,EUR,\n"
        response = client.post(
            url + "/discover",
            json={
                "source": "poe_csv",
                "csv_text": csv,
                "source_label": "cooling blanket 商品页",
                "expected_version": 0,
            },
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["items"][0]["sources"][0]["kind"] == "poe_csv"
        assert data["items"][0]["price_basis"] == "average_90d"
        reviews = [
            {
                "asin": x["asin"],
                "decision": "include" if i == 0 else "exclude",
                "reason": "人工核对",
                "price_override_reason": "",
            }
            for i, x in enumerate(data["items"])
        ]
        response = client.put(
            url + "/selection",
            json={
                "expected_version": 1,
                "reference_price": "20",
                "currency": "USD",
                "tolerance": "0.15",
                "reviews": reviews,
            },
        )
        assert response.status_code == 200, response.text
        assert [x["price_status"] for x in response.json()["items"]] == [
            "within",
            "outside",
            "unknown",
        ]
        bad = client.post(
            url + "/discover",
            json={
                "source": "poe_csv",
                "csv_text": "ASIN,Price\nINVALID,20",
                "source_label": "测试",
                "expected_version": 2,
            },
        )
        assert bad.status_code == 422
        assert client.get(url).json()["version"] == 2
    finally:
        client.close()


def test_front_discovery_distinct_keywords_and_partial_failure(db: str, engine: Engine, tmp_path: Path) -> None:
    from collections.abc import Generator
    from contextlib import contextmanager

    from app.domain.errors import DomainError
    from app.domain.verification import SearchPage
    from app.services.amazon_search import SearchSession, parse_snapshot

    client, base = ready(db, engine, tmp_path, "generic_brand")
    client.__exit__(None, None, None)

    class Search:
        def search(self, keyword: str, page_number: int, evidence_name: str) -> SearchPage:
            assert page_number == 1
            if keyword.startswith("Other"):
                raise DomainError(502, "SEARCH_ACCESS_BLOCKED", "测试访问受限")
            return parse_snapshot(
                {
                    "region": "US 10001",
                    "ready": True,
                    "next_page": False,
                    "pagination_known": True,
                    "cards": [
                        {"asin": "B000000099", "sponsored": True},
                        {"asin": "B012345678", "sponsored": False},
                        {"asin": "B012345678", "sponsored": False},
                        {
                            "asin": "B087654321" if keyword == "cooling blanket" else "B000000003",
                            "sponsored": False,
                        },
                    ],
                }
            )

        @contextmanager
        def open(self, marketplace: str, evidence_dir: Path) -> Generator[SearchSession]:
            assert marketplace == "US"
            yield self

    with TestClient(create_app(db, storage_root=tmp_path, search_provider=Search())) as api:
        response = api.post(
            base + "/competitors/discover", json={"source": "front_search", "expected_version": 0}
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert [x["asin"] for x in data["items"]] == ["B012345678"]
        assert data["items"][0]["keyword_hits"] == 2
        assert [s["position"] for s in data["items"][0]["sources"]] == [1, 1]
        assert any("2/3" in warning for warning in data["warnings"])
        invalid = api.post(
            base + "/competitors/discover",
            json={
                "source": "front_search",
                "expected_version": 1,
                "keywords": ["unconfirmed term"],
            },
        )
        assert invalid.status_code == 422
        # Repeating the same discovery cannot inflate hit counts.
        again = api.post(
            base + "/competitors/discover", json={"source": "front_search", "expected_version": 1}
        )
        assert again.json()["items"][0]["keyword_hits"] == 2


def test_front_discovery_uses_every_core_keyword_and_keeps_more_than_ten(db: str, engine: Engine, tmp_path: Path) -> None:
    from collections.abc import Generator
    from contextlib import contextmanager

    from app.domain.verification import SearchPage
    from app.services.amazon_search import SearchSession, parse_snapshot

    client, base = ready(db, engine, tmp_path, "generic_brand")
    client.__exit__(None, None, None)
    core_keywords = [f"core term {index}" for index in range(1, 7)]
    replace_core_keywords(engine, tmp_path, core_keywords)
    searched: list[str] = []

    class Search:
        def search(self, keyword: str, page_number: int, evidence_name: str) -> SearchPage:
            assert page_number == 1
            searched.append(keyword)
            cards = [{"asin": f"B{index:09d}", "sponsored": False} for index in range(1, 13)]
            if keyword in core_keywords[:2]:
                cards.append({"asin": "B999999999", "sponsored": False})
            return parse_snapshot(
                {
                    "region": "US 10001",
                    "ready": True,
                    "next_page": False,
                    "pagination_known": True,
                    "cards": cards,
                }
            )

        @contextmanager
        def open(self, marketplace: str, evidence_dir: Path) -> Generator[SearchSession]:
            assert marketplace == "US"
            yield self

    with TestClient(create_app(db, storage_root=tmp_path, search_provider=Search())) as api:
        response = api.post(
            base + "/competitors/discover",
            json={"source": "front_search", "expected_version": 0},
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert searched == core_keywords
        assert len(data["items"]) == 13
        assert [item["keyword_hits"] for item in data["items"]] == [6] * 12 + [2]


def test_front_discovery_uses_first_thirty_and_ranks_hits_before_relevance(
    db: str, engine: Engine, tmp_path: Path,
) -> None:
    from collections.abc import Generator
    from contextlib import contextmanager

    from app.domain.verification import SearchPage
    from app.services.amazon_search import SearchSession, parse_snapshot

    client, base = ready(db, engine, tmp_path, "generic_brand")
    client.__exit__(None, None, None)
    core_keywords = ["core one", "core two", "core three"]
    replace_core_keywords(engine, tmp_path, core_keywords)
    # B000000025 proves positions 21-30 are included. B000000031 must remain outside scope.
    save_product_term_scores(
        engine,
        tmp_path,
        base.split("/")[-1],
        [
            ("B000000001", "40"),
            ("B000000002", "95"),
            ("B000000025", "80"),
        ],
    )

    class Search:
        def search(self, keyword: str, page_number: int, evidence_name: str) -> SearchPage:
            cards = [{"asin": f"B{index:09d}", "sponsored": False} for index in range(1, 32)]
            if keyword == "core three":
                cards = [card for card in cards if card["asin"] != "B000000002"]
            return parse_snapshot(
                {
                    "region": "US 10001",
                    "ready": True,
                    "next_page": False,
                    "pagination_known": True,
                    "cards": cards,
                }
            )

        @contextmanager
        def open(self, marketplace: str, evidence_dir: Path) -> Generator[SearchSession]:
            yield self

    with TestClient(create_app(db, storage_root=tmp_path, search_provider=Search())) as api:
        response = api.post(
            base + "/competitors/discover",
            json={"source": "front_search", "expected_version": 0},
        )
        assert response.status_code == 200, response.text
        items = response.json()["items"]
        by_asin = {item["asin"]: item for item in items}
        assert "B000000025" in by_asin
        assert "B000000031" not in by_asin
        assert by_asin["B000000001"]["relevance_score"] == "40"
        assert by_asin["B000000002"]["relevance_score"] == "95"
        # Three keyword hits outrank a higher relevance score with only two hits.
        assert [item["asin"] for item in items].index("B000000001") < [
            item["asin"] for item in items
        ].index("B000000002")
        # For equal hit counts, four-dimensional relevance is the second sort key.
        equal_hits = [item for item in items if item["keyword_hits"] == 3]
        assert [item["asin"] for item in equal_hits[:2]] == ["B000000025", "B000000001"]


def test_details_refresh_preserves_identity_and_clears_prior_decision(db: str, engine: Engine, tmp_path: Path) -> None:
    from decimal import Decimal

    from app.domain.asin_product import ProductSnapshot

    class Products:
        def fetch(self, marketplace: str, asin: str) -> ProductSnapshot:
            return ProductSnapshot(
                asin=asin,
                title="Cooling blanket",
                price=Decimal("21.50"),
                currency="USD",
                source_url=f"https://www.amazon.com/dp/{asin}",
            )

    client, base = ready(db, engine, tmp_path)
    client.post(
        base + "/competitors/discover",
        json={"source": "manual", "asins": ["B012345678", "B0G3GRFJYB"], "expected_version": 0},
    )
    client.__exit__(None, None, None)
    with TestClient(create_app(db, storage_root=tmp_path, product_provider=Products())) as api:
        response = api.post(
            base + "/competitors/discover",
            json={"source": "details", "asins": ["B012345678"], "expected_version": 1},
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert len(data["items"]) == 1  # Own ASIN never enters competitor list.
        assert data["items"][0]["price"] == "21.50"
        payload = {
            "expected_version": 2,
            "reference_price": "20",
            "reviews": [{"asin": "B012345678", "decision": "include", "reason": "用途和主图匹配"}],
        }
        assert api.put(base + "/competitors/selection", json=payload).status_code == 200
        refreshed = api.post(
            base + "/competitors/discover",
            json={"source": "details", "asins": ["B012345678"], "expected_version": 3},
        )
        assert refreshed.json()["confirmed_at"] is None
        assert refreshed.json()["items"][0]["decision"] == "pending"


def test_product_price_parse_missing_and_ambiguous_values(db: str, engine: Engine, ) -> None:
    from decimal import Decimal

    from app.services.asin_product import parse_product_snapshot

    base: dict[str, object] = {"asin": "B012345678", "title": "Blanket", "currency": "USD"}
    assert parse_product_snapshot({**base, "price_text": "$19.99"}, "B012345678").price == Decimal(
        "19.99"
    )
    assert parse_product_snapshot({**base, "price_text": "19,99 €"}, "B012345678").price is None
    assert parse_product_snapshot(base, "B012345678").price is None


def test_currency_evidence_cannot_be_relabelled_as_marketplace_currency(db: str, engine: Engine, ) -> None:
    from app.services.asin_product import parse_product_snapshot

    base: dict[str, object] = {"asin": "B012345678", "title": "Blanket"}
    for text, declared, expected in [
        ("€19.99", "USD", None),
        ("€19.99", None, "EUR"),
        ("£19.99", None, "GBP"),
        ("$19.99", None, None),
        ("$19.99", "CAD", "CAD"),
        ("USD19.99", "CAD", None),
        ("¥1999", None, None),
    ]:
        snapshot = parse_product_snapshot(
            {**base, "price_text": text, "currency": declared}, "B012345678"
        )
        assert snapshot.currency == expected


def test_csv_rejects_conflicting_price_currency(db: str, engine: Engine, ) -> None:
    import pytest

    from app.adapters.competitor_csv import parse_competitor_csv
    from app.domain.errors import DomainError

    with pytest.raises(DomainError, match="币种"):
        parse_competitor_csv("ASIN,Price,Currency\nB012345678,€19.99,USD\n", "商品导出")
