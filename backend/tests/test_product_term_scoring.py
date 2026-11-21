"""商品词相关度的纯评分规则。"""

from decimal import Decimal
from uuid import uuid4

from app.domain.product_terms import (
    DimensionScores,
    ProductComparison,
    ProductProfile,
    TermAnalysisItem,
)
from app.services.product_term_scoring import (
    choose_terms,
    price_similarity,
    score_product,
    summarize_term,
)


def profile(
    price: str | None = "100",
    *,
    currency: str | None = "USD",
    quantity: str | None = "1",
    unit: str | None = "each",
    asin: str = "B000000001",
) -> ProductProfile:
    return ProductProfile(
        asin=asin,
        purpose=["支撑大门"],
        style_structure=["夹具与轮子"],
        specifications=["户外使用"],
        price=Decimal(price) if price is not None else None,
        currency=currency,
        package_quantity=Decimal(quantity) if quantity is not None else None,
        unit_basis=unit,
    )


def dimensions(purpose: int, style: int, specifications: int) -> DimensionScores:
    return DimensionScores(
        purpose=purpose,
        style_structure=style,
        specifications=specifications,
        purpose_reason="用途比较",
        style_structure_reason="结构比较",
        specifications_reason="规格比较",
    )


def test_price_similarity_uses_normalized_unit_price() -> None:
    reference = profile("100", quantity="2")
    same_unit_price = profile("50", quantity="1")
    assert price_similarity(reference, same_unit_price).score == Decimal("100")

    fifteen_percent = profile("115", quantity="2")
    assert price_similarity(reference, fifteen_percent).score == Decimal("85")

    double = profile("200", quantity="2")
    assert price_similarity(reference, double).score == Decimal("0")


def test_price_similarity_rejects_unreliable_comparisons() -> None:
    reference = profile()
    cases = [
        profile(None),
        profile(currency="EUR"),
        profile(quantity=None),
        profile(unit="pair"),
    ]
    for candidate in cases:
        result = price_similarity(reference, candidate)
        assert result.valid is False
        assert result.score is None
        assert result.reason


def test_weighted_score_and_strict_threshold() -> None:
    comparison = score_product(profile("100"), profile("140"), dimensions(90, 80, 70))
    assert comparison.price_score == Decimal("60")
    assert comparison.overall_score == Decimal("79.5")
    assert comparison.high_related is True

    exactly_sixty = score_product(profile(), profile(), dimensions(60, 60, 60))
    assert exactly_sixty.overall_score == Decimal("64")
    assert exactly_sixty.high_related is True

    # 价格为 0 分时，三项均为 60，综合分严格等于 54；另构造恰好 60 的权重组合。
    threshold = score_product(profile("100"), profile("200"), dimensions(70, 60, 55))
    assert threshold.overall_score == Decimal("56.5")
    assert threshold.high_related is False
    sixty = score_product(profile("100"), profile("200"), dimensions(80, 60, 55))
    assert sixty.overall_score == Decimal("60")
    assert sixty.high_related is False
    sixty_one = score_product(profile("100"), profile("200"), dimensions(80, 60, 60))
    assert sixty_one.overall_score == Decimal("61")
    assert sixty_one.high_related is True


def test_missing_dimension_excludes_but_missing_price_scores_dimensions_only() -> None:
    missing_dimension = profile()
    missing_dimension.specifications = []
    result = score_product(profile(), missing_dimension, dimensions(80, 80, 80))
    assert result.status == "excluded"
    assert result.overall_score is None

    missing_price = score_product(profile(), profile(None), dimensions(80, 80, 80))
    # 价格不可比不再剔除，只去掉价格分量：三个主维度归一化到 0-100。
    assert missing_price.status == "high_related"
    assert missing_price.price_score is None
    assert missing_price.overall_score == Decimal("80")


def comparisons(valid: int, high: int) -> list[ProductComparison]:
    values: list[ProductComparison] = []
    for index in range(valid):
        candidate = profile(asin=f"B{index:09d}")
        values.append(
            score_product(
                profile(),
                candidate,
                dimensions(100, 100, 100) if index < high else dimensions(0, 0, 0),
            )
        )
    return values


def test_summary_uses_only_valid_samples_and_confidence_boundaries() -> None:
    summary = summarize_term(comparisons(25, 18))
    assert summary.valid_samples == 25
    assert summary.high_related_count == 18
    assert summary.relevance_percent == Decimal("72")
    assert summary.confidence == "high"

    assert summarize_term(comparisons(9, 1)).confidence == "low"
    assert summarize_term(comparisons(10, 1)).confidence == "medium"
    assert summarize_term(comparisons(19, 1)).confidence == "medium"
    assert summarize_term(comparisons(20, 1)).confidence == "high"
    assert summarize_term(comparisons(30, 1)).confidence == "high"
    empty = summarize_term([])
    assert empty.confidence == "none"
    assert empty.relevance_percent is None


def test_choose_terms_ignores_failed_and_empty_and_keeps_exact_ties() -> None:
    first, second, failed, empty = uuid4(), uuid4(), uuid4(), uuid4()
    items = [
        TermAnalysisItem(candidate_id=first, text="gate wheel", status="completed", summary=summarize_term(comparisons(5, 4))),
        TermAnalysisItem(candidate_id=second, text="gate caster", status="completed", summary=summarize_term(comparisons(10, 8))),
        TermAnalysisItem(candidate_id=failed, text="wheel", status="failed"),
        TermAnalysisItem(candidate_id=empty, text="caster", status="no_samples", summary=summarize_term([])),
    ]
    assert choose_terms(items) == [first, second]


def test_choose_terms_requires_strictly_more_than_forty_percent() -> None:
    cases = [(30, 11), (30, 12), (30, 13), (10, 5), (10, 6)]
    items = [
        TermAnalysisItem(
            candidate_id=uuid4(),
            text=f"candidate {index}",
            status="completed",
            summary=summarize_term(comparisons(valid, high)),
        )
        for index, (valid, high) in enumerate(cases)
    ]
    # 12/30 is exactly 40% and must fail; 13/30 and exactly 50% now pass.
    assert choose_terms(items) == [item.candidate_id for item in items[2:]]
    items[2].status = "running"
    items[3].status = "failed"
    assert choose_terms(items) == [items[4].candidate_id]
