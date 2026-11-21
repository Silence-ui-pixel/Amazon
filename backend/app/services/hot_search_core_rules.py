"""热门搜索词核心大词的纯门控与排序；不截断结果或处理 UI 勾选态。"""

from collections.abc import Iterable
from decimal import Decimal
from typing import Literal

from app.domain.core_terms import ExactIntentEvidence, HotSearchCoreItem
from app.domain.text import fold_text


def is_exact_intent(evidence: ExactIntentEvidence) -> bool:
    return (
        evidence.category == "exact"
        and evidence.object_match
        and evidence.purpose_match
        and evidence.product_level_match
        and evidence.modifiers_compatible
        and not evidence.explicit_conflict
        and not evidence.accessory_target
        and not evidence.broad_category
    )


HotSearchRuleVersion = Literal["hot-search-core-v1", "hot-search-core-v2"]


def is_core_eligible(
    item: HotSearchCoreItem,
    rule_version: HotSearchRuleVersion = "hot-search-core-v2",
) -> bool:
    relevance_ok = (
        item.summary is not None
        and item.summary.relevance_percent is not None
        and (
            item.summary.relevance_percent > Decimal("60")
            if rule_version == "hot-search-core-v1"
            else item.summary.relevance_percent >= Decimal("50")
        )
    )
    return (
        item.status == "completed"
        and item.search_frequency_rank <= 250000
        and item.brand_category == "generic"
        and item.intent_evidence is not None
        and is_exact_intent(item.intent_evidence)
        and item.summary is not None
        and item.summary.valid_samples > 0
        and relevance_ok
    )


def sort_core_items(
    items: Iterable[HotSearchCoreItem],
    rule_version: HotSearchRuleVersion = "hot-search-core-v2",
) -> list[HotSearchCoreItem]:
    def order(item: HotSearchCoreItem) -> tuple[bool, Decimal, int, str]:
        relevance = (
            item.summary.relevance_percent
            if item.summary is not None and item.summary.relevance_percent is not None
            else Decimal("-1")
        )
        return (
            not is_core_eligible(item, rule_version),
            -relevance,
            item.search_frequency_rank,
            fold_text(item.keyword),
        )

    return sorted(items, key=order)
