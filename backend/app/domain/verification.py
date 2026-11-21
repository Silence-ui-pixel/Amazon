"""搜索核验契约：自然位置与模型推荐分开保存。"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from app.domain.asin_product import ProductSnapshot
from app.domain.schemas import Contract

ItemStatus = Literal[
    "paused",
    "waiting",
    "running",
    "matched",
    "not_found_in_scope",
    "failed",
    "uncertain",
    "needs_reference",
]


class VerificationInput(Contract):
    reference_image_id: UUID | None = None
    max_pages: int = Field(default=3, ge=1, le=3)
    candidate_ids: list[UUID] | None = Field(default=None, min_length=1, max_length=10)

    @model_validator(mode="after")
    def unique_candidates(self) -> "VerificationInput":
        if self.candidate_ids is not None and len(self.candidate_ids) != len(
            set(self.candidate_ids)
        ):
            raise ValueError("不能重复选择同一词根")
        return self


class SearchCard(Contract):
    asin: str
    image_url: str = ""
    position: int = Field(ge=1)


class SearchPage(Contract):
    region: str
    cards: list[SearchCard]
    next_page: bool
    url: str = ""
    screenshot: str = ""
    diagnostic: str = ""


class ProductCollectionFailure(Contract):
    asin: str
    reason: str
    error_code: str = ""

    @model_validator(mode="before")
    @classmethod
    def identify_legacy_asin_mismatch(cls, value: object) -> object:
        if isinstance(value, dict) and not value.get("error_code"):
            reason = value.get("reason")
            if isinstance(reason, str) and reason.startswith("商品页 ASIN 与输入不一致"):
                return {**value, "error_code": "ASIN_IDENTITY_UNCONFIRMED"}
        return value


class CollectionResult(Contract):
    term: str
    raw_natural_count: int = Field(ge=0)
    unique_asins: list[str] = Field(max_length=30)
    products: list[ProductSnapshot] = Field(max_length=30)
    failures: list[ProductCollectionFailure] = Field(
        default_factory=lambda: list[ProductCollectionFailure]()
    )
    pages: list[SearchPage] = Field(default_factory=lambda: list[SearchPage]())


class ImageComparison(Contract):
    asin: str = ""
    image_url: str
    verdict: Literal["match", "no_match", "uncertain"]
    model_verdict: str = ""
    reason: str
    reference_sha256: str
    candidate_sha256: str = ""
    pixel_equal: bool = False
    hash_similarity: float | None = Field(default=None, ge=0, le=1)
    feature_similarity: float | None = Field(default=None, ge=0, le=1)


class PageCheckpoint(Contract):
    query: str = ""
    number: int
    result: SearchPage
    processed: bool = False


class VerificationItem(Contract):
    candidate_id: UUID
    text: str
    translation: str
    expansions: list[str] = Field(default_factory=list[str])
    active_query: str = ""
    matched_query: str = ""
    completed_queries: list[str] = Field(default_factory=list[str])
    status: ItemStatus = "waiting"
    page: int | None = None
    position: int | None = None
    checked_pages: int = 0
    pages: list[PageCheckpoint] = Field(default_factory=list[PageCheckpoint])
    error_code: str = ""
    failure_stage: str = ""
    diagnostic_url: str = ""
    matched_asin: str | None = None
    match_method: Literal["asin", "main_image"] | None = None
    image_url: str = ""
    search_url: str = ""
    evidence_url: str = ""
    region: str = ""
    message: str = ""
    checked_at: datetime | None = None
    image_comparisons: list[ImageComparison] = Field(default_factory=list[ImageComparison])


class Verification(Contract):
    id: UUID
    project_id: UUID
    run_id: UUID
    created_at: datetime
    status: Literal[
        "paused", "queued", "running", "completed", "incomplete", "needs_reference", "interrupted"
    ] = "queued"
    max_pages: int
    reference_image_id: UUID | None = None
    ranking_mode: Literal["organic"] = "organic"
    region: str = ""
    items: list[VerificationItem]
    best_candidate_ids: list[UUID] = Field(default_factory=list[UUID])
    final: bool = False
    selection_saved: bool = False
    manual_candidates: bool = False
    model_outcome_pending: bool = False
    review_status: str = "closed"
    message: str = ""


class ImageVerdict(Contract):
    index: int = Field(ge=0)
    verdict: Literal["match", "no_match", "uncertain"]
    reason: str = Field(min_length=1, max_length=500)


class ImageVerdicts(Contract):
    results: list[ImageVerdict]
