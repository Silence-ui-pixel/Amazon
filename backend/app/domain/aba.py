"""ABA 周报契约；搜索频率排名与第一步自然搜索位置独立。"""

from datetime import UTC, date, datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from app.domain.schemas import Contract
from app.domain.traffic import MetricSnapshot


class AbaRequest(Contract):
    week_end: date | None = None
    query_mode: Literal["root", "asin", "product_term"] = "root"
    max_rows: int = Field(default=1000, ge=1, le=10000)

    @model_validator(mode="after")
    def validate_week(self) -> "AbaRequest":
        # 沿用卖家后台周报的周六结束口径，当前周不能伪装成已完整发布的周报。
        if self.week_end is None:
            if self.query_mode != "product_term":
                raise ValueError("week_end 必须为已结束报告周的周六")
            return self
        if self.week_end.weekday() != 5 or self.week_end >= datetime.now(UTC).date():
            raise ValueError("week_end 必须为已结束报告周的周六")
        return self


class AbaProduct(Contract):
    asin: str
    title: str | None = None
    click_share: float | None = Field(default=None, ge=0, le=1)
    conversion_share: float | None = Field(default=None, ge=0, le=1)


class AbaRow(Contract):
    keyword: str
    search_frequency_rank: int = Field(gt=0)
    products: list[AbaProduct] = Field(default_factory=list[AbaProduct])
    traffic_metrics: MetricSnapshot | None = None


class AbaQueryReport(Contract):
    raw_rows: list[dict[str, object]] = Field(
        default_factory=list[dict[str, object]], exclude=True, repr=False
    )
    query: str
    query_mode: Literal["root", "asin", "product_term"]
    total: int = Field(ge=0)
    fetched: int = Field(ge=0)
    pages: int = Field(ge=1)
    truncated: bool
    stop_reason: Literal[
        "complete", "rank_limit", "row_limit", "page_limit", "short_page", "repeated_page"
    ]
    rows: list[AbaRow]


class AbaReport(Contract):
    id: UUID
    project_id: UUID
    run_id: UUID
    source: Literal["sellercentral_internal"] = "sellercentral_internal"
    marketplace: Literal["US"] = "US"
    week_start: date
    week_end: date
    fetched_at: datetime
    roots_snapshot: list[str]
    query_terms_snapshot: list[str] = Field(default_factory=list)
    rule_version: Literal["legacy-root-v1", "hot-search-core-v1", "hot-search-core-v2"] = (
        "legacy-root-v1"
    )
    query_mode: Literal["root", "asin", "product_term"]
    max_rows: int
    reports: list[AbaQueryReport]
    stale: bool = False
    warnings: list[str] = Field(default_factory=list)


class AbaConnectionStatus(Contract):
    credentials_present: bool
    cdp_configured: bool
    # 本地配置存在不能证明会话有效；只有真实请求能验证。
    login_verified: Literal[False] = False
    source: Literal["sellercentral_internal"] = "sellercentral_internal"
