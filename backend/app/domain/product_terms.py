"""商品词四维资料、逐商品比较与候选汇总契约。"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import Field, ValidationInfo, field_validator, model_validator

from app.domain.schemas import Contract
from app.domain.verification import CollectionResult


class ProductProfile(Contract):
    asin: str = ""
    purpose: list[str] = Field(min_length=1, max_length=20)
    style_structure: list[str] = Field(min_length=1, max_length=20)
    specifications: list[str] = Field(min_length=1, max_length=30)
    price: Decimal | None = Field(default=None, gt=0)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    package_quantity: Decimal | None = Field(default=None, gt=0)
    unit_basis: str | None = Field(default=None, min_length=1, max_length=100)
    price_source: Literal["unknown", "asin_current", "user_reference"] = "unknown"


class ProductProfileGenerateRequest(Contract):
    reference_price: Decimal | None = Field(default=None, gt=0)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")


class ProductProfileRequest(Contract):
    expected_version: int = Field(ge=0)
    purpose: list[str] = Field(min_length=1, max_length=20)
    style_structure: list[str] = Field(min_length=1, max_length=20)
    specifications: list[str] = Field(min_length=1, max_length=30)
    package_quantity: Decimal = Field(gt=0)
    unit_basis: str = Field(min_length=1, max_length=100)
    reference_price: Decimal | None = Field(default=None, gt=0)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")


class ProductProfileRecord(Contract):
    project_id: UUID
    run_id: UUID
    version: int = Field(ge=1)
    status: Literal["draft", "confirmed"]
    profile: ProductProfile
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class DimensionScores(Contract):
    purpose: int = Field(ge=0, le=100)
    style_structure: int = Field(ge=0, le=100)
    specifications: int = Field(ge=0, le=100)
    purpose_reason: str = Field(min_length=1, max_length=500)
    style_structure_reason: str = Field(min_length=1, max_length=500)
    specifications_reason: str = Field(min_length=1, max_length=500)


class ScoreResult(Contract):
    valid: bool
    score: Decimal | None = Field(default=None, ge=0, le=100)
    reason: str


class ProductComparison(Contract):
    asin: str
    status: Literal["high_related", "low_related", "excluded"]
    purpose_score: int | None = None
    style_structure_score: int | None = None
    specifications_score: int | None = None
    price_score: Decimal | None = Field(default=None, ge=0, le=100)
    overall_score: Decimal | None = Field(default=None, ge=0, le=100)
    high_related: bool = False
    purpose_reason: str = ""
    style_structure_reason: str = ""
    specifications_reason: str = ""
    price_reason: str = ""
    exclusion_reason: str = ""
    natural_position: int | None = Field(default=None, ge=1, le=30)
    candidate_profile: ProductProfile | None = None


class TermSummary(Contract):
    valid_samples: int = Field(ge=0, le=30)
    high_related_count: int = Field(ge=0, le=30)
    relevance_percent: Decimal | None = Field(default=None, ge=0, le=100)
    confidence: Literal["none", "low", "medium", "high"]

    @model_validator(mode="after")
    def counts_are_consistent(self) -> "TermSummary":
        if self.high_related_count > self.valid_samples:
            raise ValueError("高相关商品数不能超过有效样本数")
        if (self.valid_samples == 0) != (self.relevance_percent is None):
            raise ValueError("无有效样本时相关度必须为空")
        return self


class TermAnalysisItem(Contract):
    candidate_id: UUID
    text: str
    status: Literal["waiting", "running", "completed", "no_samples", "failed"]
    summary: TermSummary | None = None
    comparisons: list[ProductComparison] = Field(default_factory=lambda: list[ProductComparison]())
    error: str = ""
    collection: CollectionResult | None = None
    phase: Literal["waiting", "searching", "collecting", "scoring", "completed", "failed"] = "waiting"
    search_pages: int = 0
    found_products: int = 0
    collected_products: int = 0
    collection_failures: int = 0
    scored_products: int = 0
    rejection_reason: str = ""
    evidence_loaded: bool = True
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class BatchComparisonDraft(Contract):
    asin: str
    exclusion_code: str = ""
    valid: bool = True
    exclusion_reason: str = ""
    purpose: list[str] = Field(default_factory=list, max_length=20)
    style_structure: list[str] = Field(default_factory=list, max_length=20)
    specifications: list[str] = Field(default_factory=list, max_length=30)
    package_quantity: Decimal | None = Field(default=None, gt=0)
    unit_basis: str | None = Field(default=None, max_length=100)
    scores: DimensionScores | None = None

    @field_validator("package_quantity", "unit_basis", "scores", mode="before")
    @classmethod
    def _empty_string_to_none(cls, value: object) -> object:
        # 模型对「资料缺失」偶尔返回空字符串而不是 null；统一兜底为 None，避免整批校验失败。
        return None if value == "" else value

    @field_validator("scores", mode="before")
    @classmethod
    def _excluded_null_scores(cls, value: object, info: ValidationInfo) -> object:
        # 已明确剔除的商品没有评分；全 null 对象与 null 含义相同，不能按零分纳入计算。
        if (
            info.data.get("valid") is False
            and isinstance(value, dict)
            and all(part is None for part in value.values())
        ):
            return None
        return value

    @field_validator("exclusion_reason", mode="before")
    @classmethod
    def _none_to_empty_string(cls, value: object) -> object:
        # 模型对 valid=true 的商品把 exclusion_reason 返回成 null；归一为空串。
        return "" if value is None else value

    @field_validator("purpose", "style_structure", "specifications", mode="before")
    @classmethod
    def _coerce_to_list(cls, value: object) -> object:
        # 模型偶尔把列表字段返回成 null/空串（资料不全），或单个字符串（只写了一句）。
        # 统一归一：空 → []，单个字符串 → [字符串]。
        if value is None or value == "":
            return []
        if isinstance(value, str):
            return [value]
        return value

    @model_validator(mode="after")
    def validate_outcome(self) -> "BatchComparisonDraft":
        # 有效商品只要求三个主维度与分数齐全；包装数量/计价单位常缺失，不作为剔除门槛。
        complete = bool(
            self.purpose and self.style_structure and self.specifications and self.scores
        )
        if self.valid and not complete:
            raise ValueError("有效商品必须返回完整三维资料与分数")
        if not self.valid and not self.exclusion_reason.strip():
            raise ValueError("无效商品必须说明剔除原因")
        return self


class BatchComparisonSet(Contract):
    items: list[BatchComparisonDraft] = Field(min_length=1, max_length=10)


class ProductTermAnalysis(Contract):
    id: UUID
    project_id: UUID
    run_id: UUID
    profile_version: int
    rule_version: Literal["product-term-v1"] = "product-term-v1"
    status: Literal["queued", "running", "completed", "incomplete", "failed", "stale", "awaiting_review"]
    candidate_version: int = 1
    comparison_version: str = ""
    complete: bool = False
    items: list[TermAnalysisItem]
    best_candidate_ids: list[UUID] = Field(default_factory=lambda: list[UUID]())
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
