"""Independent Excel sample verification; existing traffic contracts stay unchanged."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from app.domain.product_terms import ProductProfile
from app.domain.schemas import Contract

RULE_VERSION = "library-relevance-v1"
SAMPLE_LIMIT = 30


class RelevanceExportRequest(Contract):
    ranked_only: bool = True


class RelevanceResult(Contract):
    keyword: str
    status: Literal["waiting", "running", "completed", "failed"] = "waiting"
    valid_samples: int = Field(default=0, ge=0, le=30)
    high_related_count: int = Field(default=0, ge=0, le=30)
    ratio: Decimal | None = Field(default=None, ge=0, le=1)
    sampled: int = 0
    failures: list[str] = Field(default_factory=list[str])
    error: str = ""
    attempts: int = 0
    error_code: str = ""
    collection_seconds: float = 0
    model_seconds: float = 0

    @model_validator(mode="after")
    def consistent(self) -> "RelevanceResult":
        if self.high_related_count > self.valid_samples:
            raise ValueError("高相关数量不能超过有效核验数量")
        if self.status == "completed" and (
            not self.valid_samples or self.ratio != Decimal(self.high_related_count) / Decimal(self.valid_samples)
        ):
            raise ValueError("完成结果必须符合样本计数公式")
        return self


class RelevanceExport(Contract):
    id: UUID
    project_id: UUID
    run_id: UUID
    library_id: UUID
    fingerprint: str
    library_version: int
    rule_version: str = RULE_VERSION
    ranked_only: bool
    model: str
    model_endpoint: str
    model_protocol: str
    device_id: str = ""
    marketplace: str
    delivery_region: str
    profile: ProductProfile
    profile_version: int
    status: Literal["queued", "running", "completed", "failed"] = "queued"
    total: int = 0
    completed: int = 0
    failed: int = 0
    missing_samples: int = 0
    phase: Literal["", "collecting", "verifying", "waiting", "exporting"] = ""
    current_keyword: str = ""
    error: str = ""
    next_retry_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class UnscorableJudgment(Contract):
    asin: str
    verdict: Literal["unrelated", "insufficient_evidence"]
    reason: str = Field(min_length=1, max_length=500)


class UnscorableJudgments(Contract):
    items: list[UnscorableJudgment]


class RelevanceExportLookup(Contract):
    job: RelevanceExport | None = None
