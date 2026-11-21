"""第三步契约：证据快照、价格口径与人工决策分别保存。"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StringConstraints, model_validator

from app.domain.schemas import Asin, Contract

Reason = Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000)]
Money = Annotated[Decimal, Field(gt=0, max_digits=14, decimal_places=4)]
Currency = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
PriceStatus = Literal["unknown", "within", "outside"]


class PriceSettings(Contract):
    reference_price: Money | None = None
    currency: Currency = "USD"
    tolerance: Decimal = Field(default=Decimal("0.15"), ge=0, le=1)


class DiscoveryInput(Contract):
    expected_version: int = Field(ge=0)
    source: Literal["manual", "front_search", "poe_csv", "details"]
    asins: list[Asin] = Field(default_factory=list, max_length=100)
    keywords: list[str] = Field(default_factory=list, max_length=20)
    csv_text: str = Field(default="", max_length=2_000_000)
    source_label: str = Field(default="", max_length=200)

    @model_validator(mode="after")
    def required_source_fields(self) -> "DiscoveryInput":
        if self.source in ("manual", "details") and not self.asins:
            raise ValueError("请提供 ASIN")
        if self.source == "details" and len(self.asins) > 10:
            raise ValueError("每次最多读取 10 个商品详情")
        if self.source == "poe_csv" and (
            not self.csv_text.strip() or not self.source_label.strip()
        ):
            raise ValueError("请提供商品 CSV 与来源名称")
        return self


class CompetitorSource(Contract):
    kind: Literal["manual", "front_search", "poe_csv", "details"]
    label: str
    keyword: str = ""
    position: int | None = None
    url: str = ""
    region: str = ""
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Review(Contract):
    asin: Asin
    decision: Literal["pending", "include", "exclude"] = "pending"
    reason: Reason = ""
    price_override_reason: Reason = ""


class Competitor(Review):
    title: str = ""
    image_url: str = ""
    price: Money | None = None
    currency: Currency | None = None
    price_basis: Literal[
        "unknown", "live", "average_30d", "average_90d", "average_360d", "export"
    ] = "unknown"
    price_status: PriceStatus = "unknown"
    sources: list[CompetitorSource] = Field(default_factory=list[CompetitorSource])
    keyword_hits: int = 0
    relevance_score: Decimal | None = Field(default=None, ge=0, le=100)


class SelectionInput(PriceSettings):
    expected_version: int = Field(ge=1)
    reviews: list[Review] = Field(max_length=500)


class PoeDownloadInput(Contract):
    expected_version: int = Field(ge=0)
    keyword: str = Field(min_length=1, max_length=100)


class PoeBatchDownloadInput(Contract):
    expected_version: int = Field(ge=0)
    retry_keywords: list[str] = Field(default_factory=list, max_length=20)


class PoeIntentCandidate(Contract):
    ref: str = Field(min_length=1, max_length=2000)
    name: str = Field(min_length=1, max_length=300)
    top_keywords: list[str] = Field(default_factory=list, max_length=30)
    metrics: dict[str, str] = Field(default_factory=dict)


class PoeIntentChoice(Contract):
    candidate_ref: str = Field(min_length=1, max_length=2000)
    reason: str = Field(min_length=1, max_length=1000)
    confidence: Literal["low", "medium", "high"]


class PoeDownloadEvidence(Contract):
    id: UUID
    filename: str
    keyword: str
    sha256: str
    source_url: str = ""
    rows: int
    status: Literal["completed", "failed"] = "completed"
    stage: str = "completed"
    intent_ref: str = ""
    intent_name: str = ""
    intent_reason: str = ""
    intent_confidence: str = ""
    price_qualified_rows: int = 0
    error_code: str = ""
    error: str = ""


class Competitors(PriceSettings):
    id: UUID
    project_id: UUID
    run_id: UUID
    core_selection_id: UUID
    core_keywords: list[str]
    version: int
    created_at: datetime
    confirmed_at: datetime | None = None
    stale: bool = False
    items: list[Competitor] = Field(default_factory=list[Competitor])
    warnings: list[str] = Field(default_factory=list)

    poe_downloads: list[PoeDownloadEvidence] = Field(default_factory=list[PoeDownloadEvidence])
