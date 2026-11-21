"""第四、第五步预计算流量指标，第七步只消费并兼容旧数据。"""

import json
from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.adapters.aba import normalize_row
from app.adapters.third_party import Table, map_rows
from app.domain.aba import AbaQueryReport, AbaRow
from app.domain.cleaning import Evidence, Phrase, Source
from app.domain.decisions import TrafficDecision
from app.domain.third_party import ImportRow
from app.domain.traffic import (
    Metrics,
    MetricSnapshot,
    RuleSet,
    SourceMetricRow,
    TrafficRow,
)
from app.services.aba_expansion import _ensure_current_traffic_metrics
from app.services.traffic import phrase_metrics, source_metric_rows
from app.workflows.nodes_steps_6_8 import _automatic_priority, _row_payload


def test_source_metric_snapshot_is_versioned_and_legacy_rows_remain_valid() -> None:
    snapshot = MetricSnapshot(
        rule_id=RuleSet().id,
        metrics=Metrics(rank=120000, purchase=True, monopoly=False),
    )

    assert snapshot.rule_id == "sop-traffic-v3"
    assert snapshot.metrics.rank == 120000
    assert snapshot.metrics.purchase is True

    legacy_aba = AbaRow.model_validate(
        {"keyword": "gym", "search_frequency_rank": 120000, "products": []}
    )
    legacy_seller = ImportRow.model_validate(
        {
            "row_number": 1,
            "keyword": "gym",
            "normalized_keyword": "gym",
            "raw": {},
        }
    )
    assert legacy_aba.traffic_metrics is None
    assert legacy_seller.traffic_metrics is None


def test_step4_aba_row_precomputes_strict_v3_metrics_without_slot_compression() -> None:
    raw = {
        "st-search-term": "gym mat",
        "st-search-frequency": "120000",
        "st-top-asin-1": "B000000001",
        "st-top-asin-click-share-1": "20%",
        "st-top-asin-conversion-share-1": "12%",
        "st-top-asin-2": "B000000002",
        "st-top-asin-conversion-share-2": "12%",
        "st-top-asin-3": "B000000003",
        "st-top-asin-conversion-share-3": "12%",
    }

    row = normalize_row(raw)
    # ABA 采集进度使用 model_dump() 后交给标准 json.dumps()；新增快照必须保持该边界。
    json.dumps(row.model_dump())
    assert row.traffic_metrics is not None
    metrics = row.traffic_metrics.metrics
    assert metrics.rank == 120000 and metrics.deep_analysis is True
    assert metrics.purchase_ratio == Decimal("0.6")
    assert metrics.purchase is False  # v3 为严格大于 60%。
    assert metrics.conversion_sum3 == Decimal("0.36")
    assert metrics.monopoly is True

    missing_first = normalize_row(
        {
            **raw,
            "st-top-asin-1": "-",
            "st-top-asin-2": "B000000002",
            "st-top-asin-click-share-2": "20%",
        }
    )
    assert missing_first.traffic_metrics is not None
    assert missing_first.traffic_metrics.metrics.purchase is None
    assert missing_first.traffic_metrics.metrics.purchase_ratio is None


def test_step5_sellersprite_row_precomputes_metrics_from_its_own_source_only() -> None:
    headers = [
        "关键词",
        "ABA周排名",
        "#1点击份额",
        "#1转化份额",
        "#2转化份额",
        "#3转化份额",
    ]
    table = Table(
        headers=headers,
        rows=[["gym mat", "150001", "20", "13", "10", "10"]],
        numbers=[2],
    )
    mapping = {
        "keyword": "关键词",
        "weekly_rank": "ABA周排名",
        "top1_click_share": "#1点击份额",
        "top1_conversion_share": "#1转化份额",
        "top2_conversion_share": "#2转化份额",
        "top3_conversion_share": "#3转化份额",
    }

    row = map_rows(table, mapping, "percent")[0]
    assert row.traffic_metrics is not None
    metrics = row.traffic_metrics.metrics
    assert metrics.rank == 150001 and metrics.deep_analysis is False
    assert metrics.purchase_ratio == Decimal("0.65") and metrics.purchase is True
    assert metrics.conversion_sum3 == Decimal("0.33") and metrics.monopoly is False


def test_step7_uses_matching_precomputed_aba_snapshot_without_reparsing_raw() -> None:
    source_id = uuid4()
    parsed = AbaRow(
        keyword="gym mat",
        search_frequency_rank=120000,
        traffic_metrics=MetricSnapshot(
            rule_id="sop-traffic-v3",
            metrics=Metrics(
                rank=120000,
                purchase_ratio=Decimal("0.65"),
                purchase=True,
                monopoly=False,
                deep_analysis=True,
            ),
        ),
    )
    phrase = Phrase(
        keyword="gym mat",
        evidence=[
            Evidence(
                source_id=source_id,
                query="gym mat",
                original="gym mat",
                source_row=1,
                payload=json.dumps({"parsed": parsed.model_dump(mode="json"), "raw": {}}),
            )
        ],
    )

    result = phrase_metrics(phrase, {source_id}, RuleSet())

    assert result.purchase is True
    assert result.purchase_ratio == Decimal("0.65")
    assert result.monopoly is False


def test_step7_keeps_aba_conflict_when_raw_top3_distribution_differs() -> None:
    source_a, source_b = uuid4(), uuid4()

    def evidence(source_id: object, second: str, third: str) -> Evidence:
        raw = {
            "st-search-term": "gym mat",
            "st-search-frequency": "120000",
            "st-top-asin-1": "B000000001",
            "st-top-asin-click-share-1": "20%",
            "st-top-asin-conversion-share-1": "13%",
            "st-top-asin-2": "B000000002",
            "st-top-asin-conversion-share-2": second,
            "st-top-asin-3": "B000000003",
            "st-top-asin-conversion-share-3": third,
        }
        parsed = normalize_row(raw)
        return Evidence(
            source_id=source_id,
            query="gym mat",
            original="gym mat",
            source_row=1,
            payload=json.dumps({"parsed": parsed.model_dump(mode="json"), "raw": raw}),
        )

    phrase = Phrase(
        keyword="gym mat",
        evidence=[evidence(source_a, "10%", "5%"), evidence(source_b, "5%", "10%")],
    )

    result = phrase_metrics(phrase, {source_a, source_b}, RuleSet())

    assert result.rank is None
    assert result.purchase is None
    assert result.monopoly is None
    assert result.missing == ["同周 ABA 记录冲突"]


def test_step4_save_boundary_upgrades_legacy_agent_rows_from_raw_evidence() -> None:
    raw = {
        "st-search-term": "gym mat",
        "st-search-frequency": "120000",
        "st-top-asin-1": "B000000001",
        "st-top-asin-click-share-1": "20%",
        "st-top-asin-conversion-share-1": "13%",
        "st-top-asin-2": "B000000002",
        "st-top-asin-conversion-share-2": "10%",
        "st-top-asin-3": "B000000003",
        "st-top-asin-conversion-share-3": "5%",
    }
    report = AbaQueryReport(
        query="gym mat",
        query_mode="root",
        total=1,
        fetched=1,
        pages=1,
        truncated=False,
        stop_reason="complete",
        rows=[
            AbaRow(
                keyword="gym mat",
                search_frequency_rank=120000,
                traffic_metrics=MetricSnapshot(
                    rule_id="sop-traffic-v2",
                    metrics=Metrics(rank=1, purchase=False, monopoly=True),
                ),
            )
        ],
    )
    report.raw_rows = [raw]

    upgraded = _ensure_current_traffic_metrics(report)

    assert upgraded.rows[0].traffic_metrics is not None
    assert upgraded.rows[0].traffic_metrics.rule_id == "sop-traffic-v3"
    assert upgraded.rows[0].traffic_metrics.metrics.purchase is True
    assert upgraded.raw_rows == [raw]
    json.dumps(upgraded.model_dump())


def test_step7_recomputes_mismatched_snapshot_and_reads_seller_snapshot() -> None:
    aba_id, seller_id = uuid4(), uuid4()
    stale_snapshot = AbaRow(
        keyword="gym mat",
        search_frequency_rank=120000,
        traffic_metrics=MetricSnapshot(
            rule_id="sop-traffic-v2",
            metrics=Metrics(rank=1, purchase=True, monopoly=False),
        ),
    )
    seller_row = ImportRow(
        row_number=2,
        keyword="gym mat",
        normalized_keyword="gym mat",
        traffic_metrics=MetricSnapshot(
            rule_id="sop-traffic-v3",
            metrics=Metrics(rank=130000, purchase=True, monopoly=True),
        ),
        raw={},
    )
    phrase = Phrase(
        keyword="gym mat",
        evidence=[
            Evidence(
                source_id=aba_id,
                query="gym mat",
                original="gym mat",
                source_row=1,
                payload=json.dumps(
                    {
                        "parsed": stale_snapshot.model_dump(mode="json"),
                        "raw": {
                            "st-top-asin-1": "B000000001",
                            "st-top-asin-click-share-1": 20,
                            "st-top-asin-conversion-share-1": 12,
                            "st-top-asin-2": "B000000002",
                            "st-top-asin-conversion-share-2": 10,
                            "st-top-asin-3": "B000000003",
                            "st-top-asin-conversion-share-3": 10,
                        },
                    }
                ),
            ),
            Evidence(
                source_id=seller_id,
                query="gym mat",
                original="gym mat",
                source_row=2,
                payload=seller_row.model_dump_json(),
            ),
        ],
    )
    sources = {
        aba_id: Source(
            id=aba_id,
            provider="aba",
            direction="forward",
            query="gym mat",
            period_start=date(2026, 10, 11),
            period_end=date(2026, 10, 17),
        ),
        seller_id: Source(
            id=seller_id,
            provider="sellersprite",
            direction="forward",
            query="gym mat",
            period_start=date(2026, 10, 1),
            period_end=date(2026, 10, 30),
        ),
    }

    primary = phrase_metrics(phrase, {aba_id}, RuleSet())
    rows = source_metric_rows(phrase, sources, RuleSet())

    assert primary.rank == 120000
    assert primary.purchase_ratio == Decimal("0.60") and primary.purchase is False
    seller = next(item for item in rows if item.provider == "sellersprite")
    assert seller.metrics.rank == 130000
    assert seller.metrics.purchase is True and seller.metrics.monopoly is True


def test_model_contract_excludes_priority_and_program_assigns_it_deterministically() -> None:
    decision = TrafficDecision.model_validate(
        {
            "items": [
                {
                    "keyword": "gym mat",
                    "decision": "keep",
                    "reason": "商品类型一致",
                    "evidence_note": "关键词语义与商品用途匹配",
                }
            ]
        }
    )
    assert decision.items[0].decision == "keep"
    with pytest.raises(ValidationError):
        TrafficDecision.model_validate(
            {
                "items": [
                    {
                        "keyword": "gym mat",
                        "decision": "keep",
                        "priority": "high",
                        "reason": "商品类型一致",
                        "evidence_note": "关键词语义与商品用途匹配",
                    }
                ]
            }
        )

    def row(
        *, rank: int | None, purchase: bool | None, monopoly: bool | None
    ) -> TrafficRow:
        return TrafficRow(
            phrase=Phrase(keyword="gym mat", evidence=[]),
            metrics=Metrics(rank=rank, purchase=purchase, monopoly=monopoly),
        )

    cases: list[
        tuple[TrafficRow, Literal["keep", "exclude", "defer"], Literal["high", "normal", "low"]]
    ] = [
        (row(rank=120000, purchase=True, monopoly=False), "keep", "high"),
        (row(rank=150001, purchase=True, monopoly=False), "keep", "normal"),
        (row(rank=120000, purchase=False, monopoly=False), "keep", "low"),
        (row(rank=120000, purchase=True, monopoly=True), "keep", "low"),
        (row(rank=None, purchase=None, monopoly=None), "keep", "low"),
        (row(rank=120000, purchase=True, monopoly=False), "exclude", "low"),
        (row(rank=120000, purchase=True, monopoly=False), "defer", "low"),
    ]
    assert [_automatic_priority(item, result) for item, result, _ in cases] == [
        expected for _, _, expected in cases
    ]


def test_model_payload_reads_precomputed_source_metrics() -> None:
    source_id = uuid4()
    row = TrafficRow(
        phrase=Phrase(keyword="gym mat", evidence=[]),
        metrics=Metrics(rank=120000, purchase_ratio=Decimal("0.65"), purchase=True, monopoly=False),
        source_metrics=[
            SourceMetricRow(
                source_id=source_id,
                provider="sellersprite",
                direction="forward",
                query="gym mat",
                source_row=2,
                original="gym mat",
                metrics=Metrics(
                    rank=130000,
                    purchase_ratio=Decimal("0.70"),
                    purchase=True,
                    monopoly=True,
                ),
            )
        ],
    )

    payload = _row_payload(row)

    assert payload["purchase"] is True
    sources = payload["source_metrics"]
    assert isinstance(sources, list)
    assert sources == [
        {
            "provider": "sellersprite",
            "direction": "forward",
            "rank": 130000,
            "purchase_ratio": "0.70",
            "purchase": True,
            "monopoly": True,
            "missing": [],
        }
    ]
