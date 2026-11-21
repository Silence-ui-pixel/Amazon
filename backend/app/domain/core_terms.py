"""核心大词契约：周报排名为证据，模型只补充语义判断。"""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StrictBool, StringConstraints, model_validator

from app.domain.product_terms import ProductComparison, TermSummary
from app.domain.schemas import Contract

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
Brand = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Category = Literal["generic", "own_brand", "competitor_brand", "brand_unknown"]
IntentCategory = Literal["exact", "broad", "accessory", "unrelated"]
IntentGate = Literal["object_match", "purpose_match", "product_level_match", "modifiers_compatible"]


class ExactIntentEvidence(Contract):
    """精准门槛与可核对排除事实；事实用于确定非精准类别的优先级。"""

    object_match: StrictBool
    purpose_match: StrictBool
    product_level_match: StrictBool
    modifiers_compatible: StrictBool
    category: IntentCategory
    reasons: dict[IntentGate, Text]
    explicit_conflict: StrictBool = False
    accessory_target: StrictBool = False
    broad_category: StrictBool = False

    @model_validator(mode="after")
    def validate_intent(self) -> "ExactIntentEvidence":
        gates: dict[IntentGate, bool] = {
            "object_match": self.object_match,
            "purpose_match": self.purpose_match,
            "product_level_match": self.product_level_match,
            "modifiers_compatible": self.modifiers_compatible,
        }
        if set(self.reasons) != set(gates):
            raise ValueError("精准意图四项判断必须逐项提供理由")
        all_match = all(gates.values())
        if all_match and (self.explicit_conflict or self.accessory_target or self.broad_category):
            raise ValueError("四项精准门槛通过时不得存在非精准事实")
        if self.explicit_conflict:
            expected = "unrelated"
        elif self.accessory_target:
            expected = "accessory"
        elif self.broad_category:
            expected = "broad"
        else:
            expected = "exact" if all_match else "unrelated"
        if self.category != expected:
            raise ValueError("意图类别与四项门槛或非精准事实优先级不一致")
        return self


class HotSearchCoreItem(Contract):
    candidate_id: UUID
    keyword: Text
    search_frequency_rank: int = Field(ge=1)
    source_product_terms: list[Text] = Field(default_factory=list)
    brand_category: Category = "brand_unknown"
    intent_evidence: ExactIntentEvidence | None = None
    summary: TermSummary | None = None
    comparisons: list[ProductComparison] = Field(default_factory=list[ProductComparison])
    status: Literal["waiting", "running", "completed", "no_samples", "failed"] = "waiting"
    exclusion_reasons: list[str] = Field(default_factory=list)


class CoreRequest(Contract):
    candidate_ids: list[UUID] = Field(default_factory=list[UUID], max_length=20)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=20)
    own_brands: list[Brand] = Field(default_factory=list, max_length=20)


class CoreCandidate(Contract):
    id: UUID
    keyword: str
    search_frequency_rank: int
    matched_roots: list[str]
    root_reference: bool


class CoreCandidates(Contract):
    report_id: UUID
    total: int
    offset: int
    limit: int
    source_partial: bool
    items: list[CoreCandidate]


class CoreJudgment(Contract):
    keyword_role: Literal["core", "long_tail"]
    candidate_id: UUID
    translation: Text
    relevance: Literal["high", "medium", "low"]
    intent: Literal["exact", "broad", "accessory", "unrelated"]
    category: Category
    reason: Text


class CoreDraft(Contract):
    items: list[CoreJudgment] = Field(min_length=1, max_length=20)


class CoreTerm(CoreCandidate):
    source_product_terms: list[str] = Field(default_factory=list)
    intent_evidence: ExactIntentEvidence | None = None
    summary: TermSummary | None = None
    comparisons: list[ProductComparison] = Field(default_factory=list[ProductComparison])
    keyword_role: Literal["core", "long_tail"] = "core"
    translation: str
    relevance: Literal["high", "medium", "low"]
    intent: Literal["exact", "broad", "accessory", "unrelated"]
    category: Category
    reason: str
    recommended: bool


class CoreAnalysis(Contract):
    screened_candidates: int = 0
    relevant_candidates: int = 0
    id: UUID
    project_id: UUID
    run_id: UUID
    report_id: UUID
    status: Literal["running", "succeeded", "failed", "outcome_pending"]
    model: str
    prompt_version: str = "core-terms-v3"
    created_at: datetime
    request: CoreRequest
    total_candidates: int
    remaining_candidates: int
    source_partial: bool
    stale: bool = False
    items: list[CoreTerm] = Field(default_factory=list[CoreTerm])
    error_code: str | None = None
    error_message: str | None = None
    warnings: list[str] = Field(default_factory=list)
    source_analysis_ids: list[UUID] = Field(default_factory=list[UUID])


class CorePrefilterDraft(Contract):
    candidate_ids: list[UUID] = Field(max_length=200)


class CorePrefilter(Contract):
    candidate_ids: list[UUID]
    screened_candidates: int


class CoreSelectionInput(Contract):
    analysis_id: UUID
    rule_version: Literal["core-terms-v1", "hot-search-core-v1", "hot-search-core-v2"] = (
        "core-terms-v1"
    )
    candidate_ids: list[UUID] = Field(min_length=1, max_length=10000)
    expected_version: int = Field(ge=0)

    @model_validator(mode="after")
    def unique_ids(self) -> "CoreSelectionInput":
        if len(self.candidate_ids) != len(set(self.candidate_ids)):
            raise ValueError("候选 ID 不得重复")
        return self


class CoreSelection(Contract):
    id: UUID
    project_id: UUID
    run_id: UUID
    analysis_id: UUID
    rule_version: Literal["core-terms-v1", "hot-search-core-v1", "hot-search-core-v2"] = (
        "core-terms-v1"
    )
    source_analysis_id: UUID | None = None
    report_id: UUID
    version: int
    confirmed_at: datetime
    items: list[CoreTerm] = Field(max_length=10000)
    stale: bool = False
