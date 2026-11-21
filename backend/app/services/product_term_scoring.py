"""商品词相关度纯计算；网络和模型层不得改写这些规则。"""

from decimal import Decimal
from uuid import UUID

from app.domain.product_terms import (
    DimensionScores,
    ProductComparison,
    ProductProfile,
    ScoreResult,
    TermAnalysisItem,
    TermSummary,
)

PURPOSE_WEIGHT = Decimal("0.35")
STYLE_WEIGHT = Decimal("0.35")
SPECIFICATIONS_WEIGHT = Decimal("0.20")
PRICE_WEIGHT = Decimal("0.10")
HIGH_RELATED_THRESHOLD = Decimal("60")
PRODUCT_TERM_RELEVANCE_THRESHOLD = Decimal("40")


def price_similarity(reference: ProductProfile, candidate: ProductProfile) -> ScoreResult:
    if reference.price is None or candidate.price is None:
        return ScoreResult(valid=False, reason="本品或搜索商品缺少可靠价格")
    if not reference.currency or not candidate.currency:
        return ScoreResult(valid=False, reason="本品或搜索商品缺少明确币种")
    if reference.currency != candidate.currency:
        return ScoreResult(valid=False, reason="币种不同且未提供可靠换汇依据")
    if reference.package_quantity is None or candidate.package_quantity is None:
        return ScoreResult(valid=False, reason="包装数量不完整，无法归一单件价格")
    if not reference.unit_basis or not candidate.unit_basis:
        return ScoreResult(valid=False, reason="计价单位不完整")
    if reference.unit_basis.casefold() != candidate.unit_basis.casefold():
        return ScoreResult(valid=False, reason="计价单位不同，无法可靠比较")
    reference_unit = reference.price / reference.package_quantity
    candidate_unit = candidate.price / candidate.package_quantity
    difference = abs(candidate_unit - reference_unit) / reference_unit * Decimal("100")
    score = max(Decimal("0"), Decimal("100") - difference)
    return ScoreResult(
        valid=True,
        score=score,
        reason=f"同币种同单位的归一价格差为 {difference.quantize(Decimal('0.01'))}%",
    )


def score_product(
    reference: ProductProfile,
    candidate: ProductProfile,
    dimensions: DimensionScores,
    *,
    high_related_threshold: Decimal = HIGH_RELATED_THRESHOLD,
    inclusive: bool = False,
) -> ProductComparison:
    missing: list[str] = []
    for label, value in (
        ("本品用途", reference.purpose),
        ("本品款式结构", reference.style_structure),
        ("本品规格", reference.specifications),
        ("搜索商品用途", candidate.purpose),
        ("搜索商品款式结构", candidate.style_structure),
        ("搜索商品规格", candidate.specifications),
    ):
        if not value:
            missing.append(label)
    price = price_similarity(reference, candidate)
    # 三个主维度缺失仍剔除；价格不可比（缺币种/包装数量/计价单位）不再剔除，
    # 只去掉价格分量——价格权重仅 10%，不该因一个次要字段缺失就丢掉整个商品。
    if missing:
        return ProductComparison(
            asin=candidate.asin,
            status="excluded",
            exclusion_reason=f"{', '.join(missing)}缺失",
        )
    dimension = (
        Decimal(dimensions.purpose) * PURPOSE_WEIGHT
        + Decimal(dimensions.style_structure) * STYLE_WEIGHT
        + Decimal(dimensions.specifications) * SPECIFICATIONS_WEIGHT
    )
    if price.valid and price.score is not None:
        overall = dimension + price.score * PRICE_WEIGHT
    else:
        overall = dimension / (PURPOSE_WEIGHT + STYLE_WEIGHT + SPECIFICATIONS_WEIGHT)
    high = overall >= high_related_threshold if inclusive else overall > high_related_threshold
    return ProductComparison(
        asin=candidate.asin,
        status="high_related" if high else "low_related",
        purpose_score=dimensions.purpose,
        style_structure_score=dimensions.style_structure,
        specifications_score=dimensions.specifications,
        price_score=price.score,
        overall_score=overall,
        high_related=high,
        purpose_reason=dimensions.purpose_reason,
        style_structure_reason=dimensions.style_structure_reason,
        specifications_reason=dimensions.specifications_reason,
        price_reason=price.reason,
    )


def summarize_term(comparisons: list[ProductComparison]) -> TermSummary:
    valid = [item for item in comparisons if item.status != "excluded"]
    count = len(valid)
    high = sum(item.high_related for item in valid)
    if count == 0:
        return TermSummary(
            valid_samples=0,
            high_related_count=0,
            relevance_percent=None,
            confidence="none",
        )
    confidence = "high" if count >= 20 else "medium" if count >= 10 else "low"
    return TermSummary(
        valid_samples=count,
        high_related_count=high,
        relevance_percent=Decimal(high) / Decimal(count) * Decimal("100"),
        confidence=confidence,
    )


def choose_terms(items: list[TermAnalysisItem]) -> list[UUID]:
    """选择全部相关度严格大于 40% 的已完成商品词。"""
    return [
        item.candidate_id
        for item in items
        if item.status == "completed"
        and item.summary is not None
        and item.summary.relevance_percent is not None
        and item.summary.relevance_percent > PRODUCT_TERM_RELEVANCE_THRESHOLD
    ]
