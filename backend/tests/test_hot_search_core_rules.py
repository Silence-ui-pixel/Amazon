"""Core gates reject boundary, incomplete, and contradictory evidence."""

from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.domain.core_terms import CoreTerm, ExactIntentEvidence, HotSearchCoreItem
from app.domain.product_terms import ProductComparison, TermSummary
from app.services.hot_search_core_rules import (
    is_core_eligible,
    is_exact_intent,
    sort_core_items,
)

GATES = ("object_match", "purpose_match", "product_level_match", "modifiers_compatible")


def evidence(**changes: object) -> ExactIntentEvidence:
    values: dict[str, object] = dict.fromkeys(GATES, True)
    values.update(category="exact", reasons={gate: "与已确认商品资料一致" for gate in GATES})
    values.update(changes)
    return ExactIntentEvidence.model_validate(values)


def item(**changes: object) -> HotSearchCoreItem:
    values: dict[str, object] = {
        "candidate_id": uuid4(),
        "keyword": "gate wheel",
        "search_frequency_rank": 250000,
        "source_product_terms": ["gate caster"],
        "brand_category": "generic",
        "intent_evidence": evidence(),
        "summary": TermSummary(
            valid_samples=25,
            high_related_count=16,
            relevance_percent=Decimal("64"),
            confidence="high",
        ),
        "comparisons": [
            ProductComparison(asin="B000000001", status="excluded", exclusion_reason="价格不可比")
        ],
        "status": "completed",
    }
    values.update(changes)
    return HotSearchCoreItem.model_validate(values)


@pytest.mark.parametrize(("rank", "expected"), [(250000, True), (250001, False)])
def test_rank_boundary(rank: int, expected: bool) -> None:
    # Catches a strict rank comparison or a missing rank gate.
    assert is_core_eligible(item(search_frequency_rank=rank)) is expected


@pytest.mark.parametrize(
    ("percent", "expected"),
    [
        ("60", False),
        ("60.01", True),
        ("60.0000000000000001", True),
    ],
)
def test_relevance_uses_strict_unrounded_decimal(percent: str, expected: bool) -> None:
    summary = TermSummary(
        valid_samples=25,
        high_related_count=16,
        relevance_percent=Decimal(percent),
        confidence="high",
    )
    assert is_core_eligible(item(summary=summary)) is expected


@pytest.mark.parametrize("gate", GATES)
def test_each_intent_gate_is_required(gate: str) -> None:
    intent = evidence(**{gate: False, "category": "unrelated"})
    assert is_exact_intent(intent) is False
    assert is_core_eligible(item(intent_evidence=intent)) is False


@pytest.mark.parametrize(
    ("category", "expected"),
    [
        ("generic", True),
        ("own_brand", False),
        ("competitor_brand", False),
        ("brand_unknown", False),
    ],
)
def test_brand_is_an_independent_gate(category: str, expected: bool) -> None:
    assert is_core_eligible(item(brand_category=category)) is expected


@pytest.mark.parametrize("status", ["waiting", "running", "failed", "no_samples"])
def test_unfinished_items_cannot_qualify(status: str) -> None:
    assert is_core_eligible(item(status=status)) is False


def test_missing_or_empty_evidence_cannot_qualify() -> None:
    empty = TermSummary(
        valid_samples=0, high_related_count=0, relevance_percent=None, confidence="none"
    )
    assert is_core_eligible(item(summary=empty)) is False
    assert is_core_eligible(item(summary=None)) is False
    assert is_core_eligible(item(intent_evidence=None)) is False


def test_all_forty_eligible_items_are_returned_without_mutating_input() -> None:
    items = [item(keyword=f"term {index:02}") for index in range(39, -1, -1)]
    result = sort_core_items(items)
    assert len(result) == 40
    assert all(is_core_eligible(value) for value in result)
    assert result[0].keyword == "term 00"
    assert result[-1].keyword == "term 39"
    assert items[0].keyword == "term 39"


def test_sort_uses_eligibility_relevance_rank_and_normalized_keyword() -> None:
    high = item(
        keyword="high",
        search_frequency_rank=200,
        summary=TermSummary(
            valid_samples=25,
            high_related_count=20,
            relevance_percent=Decimal("80"),
            confidence="high",
        ),
    )
    alpha = item(keyword="Ａlpha", search_frequency_rank=10)
    beta = item(keyword="beta", search_frequency_rank=10)
    worse_rank = item(keyword="aaa", search_frequency_rank=11)
    brand = item(keyword="brand", brand_category="own_brand", summary=high.summary)
    empty = item(keyword="empty", summary=None)
    assert [
        value.keyword for value in sort_core_items([empty, brand, worse_rank, beta, alpha, high])
    ] == ["high", "Ａlpha", "beta", "aaa", "brand", "empty"]


@pytest.mark.parametrize("missing", [*GATES, "reasons"])
def test_intent_requires_all_gates_and_per_gate_reasons(missing: str) -> None:
    data = evidence().model_dump()
    del data[missing]
    with pytest.raises(ValidationError):
        ExactIntentEvidence.model_validate(data)


@pytest.mark.parametrize(
    "reasons",
    [
        {},
        {gate: "" for gate in GATES},
        {gate: "证据一致" for gate in GATES if gate != "object_match"},
        {gate: "   " for gate in GATES},
    ],
)
def test_incomplete_or_blank_reasons_are_rejected(reasons: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        evidence(reasons=reasons)


@pytest.mark.parametrize("gate", GATES)
def test_exact_category_cannot_hide_a_failed_gate(gate: str) -> None:
    with pytest.raises(ValidationError):
        evidence(**{gate: False})


@pytest.mark.parametrize(
    ("facts", "category"),
    [
        (
            {"explicit_conflict": True, "accessory_target": True, "broad_category": True},
            "unrelated",
        ),
        ({"accessory_target": True, "broad_category": True}, "accessory"),
        ({"broad_category": True}, "broad"),
        ({}, "unrelated"),
    ],
)
def test_nonexact_category_obeys_semantic_fact_priority(
    facts: dict[str, bool], category: str
) -> None:
    value = evidence(object_match=False, product_level_match=False, category=category, **facts)
    assert value.category == category
    for wrong in {"exact", "broad", "accessory", "unrelated"} - {category}:
        with pytest.raises(ValidationError):
            evidence(object_match=False, product_level_match=False, category=wrong, **facts)


def test_all_true_gates_require_exact_without_conflicting_facts() -> None:
    assert is_exact_intent(evidence()) is True
    with pytest.raises(ValidationError):
        evidence(category="unrelated")
    with pytest.raises(ValidationError):
        evidence(explicit_conflict=True)


def test_legacy_core_term_json_still_parses_with_new_field_defaults() -> None:
    legacy = dict(
        id=str(uuid4()),
        keyword="gate wheel",
        search_frequency_rank=1,
        matched_roots=["wheel"],
        root_reference=True,
        translation="门轮",
        relevance="high",
        intent="exact",
        category="generic",
        reason="购买对象一致",
        recommended=True,
    )
    restored = CoreTerm.model_validate_json(__import__("json").dumps(legacy))
    assert restored.intent_evidence is None
    assert restored.summary is None
    assert restored.comparisons == []
    assert restored.source_product_terms == []
