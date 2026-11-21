"""流量筛选：抓住边界比较、缺失值误判及跨来源拼接。"""

from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from test_aba import auth_file, row
from test_aba_expansion import prepare, wait
from test_third_party import MAPPING, preview

from app.adapters.aba import AbaClient
from app.main import create_app


@pytest.mark.parametrize(
    ("click", "conversion", "third", "buy", "monopoly"),
    [
        ("0.5", "0.30", "0.02", False, True),
        ("0.5", "0.3000001", "0.02", True, True),
        ("0.5", "0.25", "0.05", False, False),
        ("0.5", "0.25", "0.0500001", False, True),
        ("0", "0.1", "0.1", None, False),
        (None, None, None, None, None),
    ],
)
def test_strict_metrics(
    click: str | None,
    conversion: str | None,
    third: str | None,
    buy: bool | None,
    monopoly: bool | None,
) -> None:
    from app.domain.traffic import MetricInput, RuleSet, evaluate

    def value(x: str | None) -> Decimal | None:
        return Decimal(x) if x is not None else None

    result = evaluate(
        MetricInput(
            rank=200001,
            click=value(click),
            conversion=value(conversion),
            conversion2=Decimal("0.05"),
            conversion3=value(third),
        ),
        RuleSet(),
    )
    assert result.purchase is buy
    assert result.monopoly is monopoly
    assert result.deep_analysis is False
    assert result.rank == 200001


def test_price_delta_ratio_must_be_a_fraction(db: str, ) -> None:
    from pydantic import ValidationError

    from app.domain.traffic import RelevanceEvidence

    assert RelevanceEvidence(price_delta_ratio=Decimal("1")).price_delta_ratio == Decimal("1")
    with pytest.raises(ValidationError):
        RelevanceEvidence(price_delta_ratio=Decimal("1.01"))


def test_relevance_boundaries_and_missing(db: str, ) -> None:
    from app.domain.traffic import RelevanceEvidence, RuleSet, relevance

    assert (
        relevance(
            RelevanceEvidence(
                style_similar=True,
                price_delta_ratio=Decimal("0.15"),
                first_half_result_share=Decimal("0.50"),
            ),
            RuleSet(),
        )
        == "high"
    )
    assert (
        relevance(
            RelevanceEvidence(
                style_similar=True,
                price_delta_ratio=Decimal("0.150001"),
                first_half_result_share=Decimal("0.50"),
            ),
            RuleSet(),
        )
        == "low"
    )
    assert relevance(RelevanceEvidence(), RuleSet()) == "unknown"


def test_screening_review_conflict_restore_and_cleaning_invalidation(
    db: str, engine: Engine, tmp_path: Path
) -> None:
    base = prepare(db, engine, tmp_path)
    aba = AbaClient(
        auth_file(tmp_path),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "reportsV2": [
                        {"totalItems": 1, "rows": [row("portable cooling blankets", 200001)]}
                    ]
                },
            )
        ),
    )
    with TestClient(create_app(db, storage_root=tmp_path, aba_client=aba)) as api:
        for direction in ("forward", "reverse"):
            task = api.post(
                base + "/aba-expansion",
                json={"direction": direction, "week_end": "2026-10-17", "max_rows": 100},
            ).json()
            assert wait(api, base + "/aba-expansion", task["id"])["status"] == "succeeded"
        for direction, query in [
            ("forward", "cooling blanket"),
            ("reverse", "B012345678"),
            ("reverse", "B087654321"),
        ]:
            imported = preview(api, base + "/third-party", query, direction)
            assert (
                api.post(
                    base + f"/third-party/{imported['id']}/confirm",
                    json={"mapping": MAPPING, "share_unit": "percent"},
                ).status_code
                == 200
            )
        clean = api.post(
            base + "/cleaning",
            json={
                "aba_week_end": "2026-10-17",
                "seller_start": "2026-10-01",
                "seller_end": "2026-10-31",
                "text_only_acknowledged": True,
            },
        ).json()
        request = {"cleaning_id": clean["id"], "cleaning_version": 1}
        url = base + "/traffic"
        assert api.post(url, json=request).status_code == 409
        cleaning_review = {
            "expected_version": 0,
            "roots": [{"term": "portable", "category": "preference"}],
            "exclusions": [{"keyword": "=1+1", "reason": "噪声"}],
            "reason": "分类核对",
        }
        assert (
            api.put(base + f"/cleaning/{clean['id']}/review", json=cleaning_review).status_code
            == 200
        )
        response = api.post(url, json=request)
        assert response.status_code == 200, response.text
        report = response.json()
        key = report["id"]
        assert report["total"] == 3 and report["required"] == 2 and report["complete"] is False
        assert api.post(url, json=request).json()["id"] == key
        page = api.get(url + f"/{key}/rows?limit=1&offset=1").json()
        assert page["total"] == 3 and len(page["items"]) == 1
        rows = api.get(url + f"/{key}/rows").json()["items"]
        third_party_only = next(r for r in rows if r["phrase"]["keyword"] == "cooling blanket")
        assert third_party_only["metrics"]["purchase"] is None
        assert third_party_only["metrics"]["rank"] is None
        references = third_party_only["references"]
        assert len(references) == 3
        assert {r["direction"] for r in references} == {"forward", "reverse"}
        assert all(r["period_start"] == "2026-10-01" for r in references)
        assert all(r["period_end"] == "2026-10-31" for r in references)
        assert all(r["monthly_searches"] == 1200 for r in references)
        assert all(r["weekly_rank"] is None for r in references)
        aba_only = next(r for r in rows if r["phrase"]["keyword"] == "portable cooling blankets")
        assert aba_only["metrics"]["rank"] == 200001
        assert aba_only["references"] == []
        review = {
            "expected_version": 0,
            "keyword": "cooling blanket",
            "decision": "keep",
            "priority": "high",
            "style_similar": True,
            "price_delta_ratio": "0.15",
            "first_half_result_share": "0.50",
            "reason": "产品匹配",
            "evidence_note": "前半页 10 个结果中 5 个同款，价格浮动 15%",
            "reviewer": "本地运营",
        }
        saved = api.put(url + f"/{key}/review", json=review)
        assert saved.status_code == 200, saved.text
        assert saved.json()["reviewed"] == 1 and saved.json()["complete"] is False
        assert api.put(url + f"/{key}/review", json=review).status_code == 409
        assert (
            api.put(
                url + f"/{key}/review", json={**review, "expected_version": 1, "keyword": "=1+1"}
            ).status_code
            == 422
        )
        final = api.put(
            url + f"/{key}/review",
            json={
                **review,
                "expected_version": 1,
                "keyword": "portable cooling blankets",
                "decision": "exclude",
            },
        ).json()
        assert final["complete"] is True and final["version"] == 2
        csv = api.get(url + f"/{key}/export").text
        assert "'=1+1" in csv and "sop-traffic-v3" in csv and "本地运营" in csv
        history = api.get(url + f"/{key}/reviews").json()
        assert len(history) == 2 and history[0]["relevance"] == "high"
        other = api.post("/api/v1/projects", json={"name": "other", "marketplace": "US"}).json()[
            "id"
        ]
        foreign = url.replace(base.split("/")[4], other)
        assert api.get(foreign + f"/{key}/rows").status_code == 404
    with TestClient(create_app(db, storage_root=tmp_path)) as api:
        assert api.get(url).json()[0]["complete"] is True
        assert (
            api.put(
                base + f"/cleaning/{clean['id']}/review",
                json={**cleaning_review, "expected_version": 1, "reason": "重新核对"},
            ).status_code
            == 200
        )
        historical = api.get(url + f"/{key}").json()
        assert historical["stale"] is True and historical["complete"] is False
        assert (
            api.put(url + f"/{key}/review", json={**review, "expected_version": 2}).status_code
            == 409
        )
        assert api.get(url + f"/{key}/export").status_code == 200
        regenerated = api.post(url, json={**request, "cleaning_version": 2}).json()
        assert regenerated["id"] != key and regenerated["version"] == 0


@pytest.mark.parametrize("conflicting", [False, True])
def test_aba_duplicates_conflicts_and_third_party_isolation(db: str, conflicting: bool) -> None:
    import json
    from uuid import uuid4

    from app.domain.aba import AbaProduct, AbaRow
    from app.domain.cleaning import Evidence, Phrase
    from app.domain.traffic import RuleSet
    from app.services.traffic import phrase_metrics

    aba_id, seller_id = uuid4(), uuid4()
    record = AbaRow(
        keyword="gym",
        search_frequency_rank=150000,
        products=[
            AbaProduct(asin="B012345678", click_share=0.1, conversion_share=0.2),
            AbaProduct(asin="B087654321", click_share=0.05, conversion_share=0.05),
            AbaProduct(asin="B000000003", click_share=0.01, conversion_share=0.02),
        ],
    )
    other = record.model_copy(update={"search_frequency_rank": 150001}) if conflicting else record
    phrase = Phrase(
        keyword="gym",
        evidence=[
            Evidence(
                source_id=aba_id,
                query="gym",
                original="gym",
                source_row=i,
                payload=json.dumps(
                    {
                        "parsed": r.model_dump(),
                        "raw": {
                            "st-top-asin-1": "B012345678",
                            "st-top-asin-click-share-1": 10,
                            "st-top-asin-conversion-share-1": 20,
                            "st-top-asin-2": "B087654321",
                            "st-top-asin-conversion-share-2": 5,
                            "st-top-asin-3": "B000000003",
                            "st-top-asin-conversion-share-3": 2,
                        },
                    }
                ),
            )
            for i, r in enumerate([record, other])
        ]
        + [
            Evidence(
                source_id=seller_id,
                query="gym",
                original="gym",
                source_row=1,
                payload='{"monthly_rank": 1, "conversion_share": 0.99}',
            )
        ],
    )
    result = phrase_metrics(phrase, {aba_id}, RuleSet())
    if conflicting:
        assert result.purchase is None and result.rank is None
        assert result.missing == ["同周 ABA 记录冲突"]
    else:
        assert result.purchase_ratio == Decimal("2")
        assert result.purchase is True and result.monopoly is False
        assert result.deep_analysis is True and result.rank == 150000


def test_reference_records_keep_third_party_values_out_of_metrics(db: str, ) -> None:
    import json
    from datetime import date
    from uuid import uuid4

    from app.domain.cleaning import Evidence, Phrase, Source
    from app.services.traffic import reference_records

    seller = uuid4()
    source = Source(
        id=seller,
        provider="sellersprite",
        direction="forward",
        query="gym",
        period_start=date(2026, 9, 1),
        period_end=date(2026, 9, 30),
    )
    payload = json.dumps(
        {
            "row_number": 2,
            "keyword": "gym",
            "normalized_keyword": "gym",
            "weekly_rank": 400000,
            "monthly_searches": 1200,
            "raw": {},
        }
    )
    evidence = Evidence(
        source_id=seller, query="gym", original="gym", source_row=2, payload=payload
    )
    records = reference_records(
        Phrase(keyword="gym", evidence=[evidence, evidence]), {seller}, {seller: source}
    )
    assert len(records) == 1
    assert records[0].weekly_rank == 400000
    assert records[0].monthly_searches == 1200
    assert records[0].period_start == date(2026, 9, 1)
    assert records[0].period_end == date(2026, 9, 30)


def test_source_metric_rows_keep_each_source_calculation_separate(db: str, ) -> None:
    import json
    from datetime import date
    from uuid import uuid4

    from app.domain.aba import AbaProduct, AbaRow
    from app.domain.cleaning import Evidence, Phrase, Source
    from app.domain.traffic import RuleSet
    from app.services.traffic import source_metric_rows

    aba_forward, aba_reverse, seller_forward, seller_reverse = [uuid4() for _ in range(4)]
    sources = {
        aba_forward: Source(
            id=aba_forward,
            provider="aba",
            direction="forward",
            query="gym",
            period_start=date(2026, 10, 11),
            period_end=date(2026, 10, 17),
        ),
        aba_reverse: Source(
            id=aba_reverse,
            provider="aba",
            direction="reverse",
            query="B012345678",
            period_start=date(2026, 10, 11),
            period_end=date(2026, 10, 17),
        ),
        seller_forward: Source(
            id=seller_forward,
            provider="sellersprite",
            direction="forward",
            query="gym",
            period_start=date(2026, 10, 1),
            period_end=date(2026, 10, 30),
        ),
        seller_reverse: Source(
            id=seller_reverse,
            provider="sellersprite",
            direction="reverse",
            query="B012345678",
            period_start=date(2026, 10, 1),
            period_end=date(2026, 10, 30),
        ),
    }
    aba_row = AbaRow(
        keyword="gym",
        search_frequency_rank=120000,
        products=[
            AbaProduct(asin="B012345678", click_share=0.2, conversion_share=0.12),
            AbaProduct(asin="B087654321", click_share=0.1, conversion_share=0.12),
            AbaProduct(asin="B000000003", click_share=0.1, conversion_share=0.12),
        ],
    )
    aba_payload = json.dumps(
        {
            "parsed": aba_row.model_dump(mode="json"),
            "raw": {
                "st-top-asin-1": "B012345678",
                "st-top-asin-click-share-1": 20,
                "st-top-asin-conversion-share-1": 12,
                "st-top-asin-2": "B087654321",
                "st-top-asin-conversion-share-2": 12,
                "st-top-asin-3": "B000000003",
                "st-top-asin-conversion-share-3": 12,
            },
        }
    )
    seller_full = json.dumps(
        {
            "row_number": 8,
            "keyword": "gym",
            "normalized_keyword": "gym",
            "weekly_rank": 130000,
            "top1_click_share": "0.20",
            "top1_conversion_share": "0.12",
            "top2_conversion_share": "0.12",
            "top3_conversion_share": "0.12",
            "raw": {},
        }
    )
    seller_missing = json.dumps(
        {
            "row_number": 9,
            "keyword": "gym",
            "normalized_keyword": "gym",
            "weekly_rank": None,
            "raw": {},
        }
    )
    phrase = Phrase(
        keyword="gym",
        evidence=[
            Evidence(source_id=aba_forward, query="gym", original="gym", source_row=2, payload=aba_payload),
            Evidence(source_id=aba_reverse, query="B012345678", original="gym", source_row=3, payload=aba_payload),
            Evidence(source_id=seller_forward, query="gym", original="gym", source_row=8, payload=seller_full),
            Evidence(source_id=seller_reverse, query="B012345678", original="gym", source_row=9, payload=seller_missing),
        ],
    )

    result = source_metric_rows(phrase, sources, RuleSet())

    assert [(row.provider, row.direction, row.source_row) for row in result] == [
        ("aba", "forward", 2),
        ("aba", "reverse", 3),
        ("sellersprite", "forward", 8),
        ("sellersprite", "reverse", 9),
    ]
    assert result[0].metrics.purchase_ratio == Decimal("0.60")
    assert result[0].metrics.purchase is False
    assert result[0].metrics.conversion_sum3 == Decimal("0.36")
    assert result[0].metrics.monopoly is True
    assert result[2].metrics.purchase is False and result[2].metrics.monopoly is True
    assert result[3].metrics.purchase is None and result[3].metrics.monopoly is None
    assert "首位点击份额缺失或为零" in result[3].metrics.missing[1]


def test_missing_third_product_not_filled_by_another_record(db: str, ) -> None:
    from app.domain.traffic import MetricInput, RuleSet, evaluate

    result = evaluate(
        MetricInput(click=Decimal("0.5"), conversion=Decimal("0.4"), conversion2=Decimal("0.1")),
        RuleSet(),
    )
    assert result.purchase is True
    assert result.monopoly is True and result.conversion_sum3 is None
    assert "前三产品转化份额不完整" in result.missing


@pytest.mark.parametrize("missing_first", [False, True])
def test_original_percentage_and_product_slots(db: str, missing_first: bool) -> None:
    import json
    from uuid import uuid4

    from app.adapters.aba import normalize_row
    from app.domain.cleaning import Evidence, Phrase
    from app.domain.traffic import RuleSet
    from app.services.traffic import phrase_metrics

    raw = row("gym")
    raw.update({"st-top-asin-click-share-1": "0.35%", "st-top-asin-conversion-share-1": "0.21%"})
    if missing_first:
        raw.update(
            {
                "st-top-asin-1": "-",
                "st-top-asin-2": "B012345678",
                "st-top-asin-click-share-2": 10,
                "st-top-asin-conversion-share-2": 5,
            }
        )
    key = uuid4()
    phrase = Phrase(
        keyword="gym",
        evidence=[
            Evidence(
                source_id=key,
                query="gym",
                original="gym",
                source_row=0,
                payload=json.dumps({"raw": raw, "parsed": normalize_row(raw).model_dump()}),
            )
        ],
    )
    result = phrase_metrics(phrase, {key}, RuleSet())
    if missing_first:
        assert result.purchase is None and result.purchase_ratio is None
    else:
        assert result.purchase_ratio == Decimal("0.60")
        assert result.purchase is False
