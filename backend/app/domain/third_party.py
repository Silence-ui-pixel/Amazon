"""第三方词表导入契约：保留原文、单位、来源与不可变确认。"""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from app.domain.aba_expansion import Direction
from app.domain.schemas import Contract
from app.domain.sellersprite import CollectionMethod, VariantScope
from app.domain.traffic import MetricSnapshot


class ImportMetadata(Contract):
    direction: Direction
    query: str = Field(min_length=1, max_length=300)
    collection_method: CollectionMethod | None = None
    source_queries: list[str] = Field(default_factory=list, max_length=20)
    variant_scope: VariantScope | None = None
    marketplace: str
    period_kind: Literal["date_range", "last_30_days"] = "date_range"
    period_start: date | None = None
    period_end: date | None = None
    collected_at: datetime | None = None
    source_total: int | None = Field(default=None, ge=0, le=10_000_000)
    export_complete: bool = False
    # 导出被供应商单次上限截断：行数少于页面结果总数，但已确认可用。
    truncated: bool = False
    batch_key: str = Field(default="", max_length=64)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")

    @model_validator(mode="after")
    def validate_period(self) -> Self:
        method = self.collection_method or (
            "keyword_mining" if self.direction == "forward" else "keyword_reverse"
        )
        expected_direction = "forward" if method == "keyword_mining" else "reverse"
        if self.direction != expected_direction:
            raise ValueError("来源采集方式与正向/反向方向不一致")
        queries = self.source_queries or [self.query]
        normalized = [value.strip() for value in queries if value.strip()]
        if method == "traffic_extension":
            if not 1 <= len(normalized) <= 20 or self.variant_scope is None:
                raise ValueError("拓展流量词来源须记录 1 至 20 个 ASIN 和变体范围")
        elif self.variant_scope is not None or len(normalized) != 1:
            raise ValueError("单查询来源不能记录批量变体范围")
        self.collection_method = method
        self.source_queries = normalized
        if self.period_kind == "last_30_days":
            if (
                self.period_start is not None
                or self.period_end is not None
                or self.collected_at is None
            ):
                raise ValueError("最近30天保留采集时间，不得伪造固定起止日期")
            return self
        if self.period_start is None or self.period_end is None:
            raise ValueError("固定周期须填写报告起止日期")
        if self.period_start > self.period_end:
            raise ValueError("报告开始日不能晚于结束日")
        return self


class ImportConfirmation(Contract):
    mapping: dict[str, str]
    share_unit: Literal["fraction", "percent"]


class ImportRow(Contract):
    row_number: int
    keyword: str
    normalized_keyword: str
    translation: str | None = None
    monthly_searches: int | None = None
    monthly_rank: int | None = None
    weekly_rank: int | None = None
    bid: Decimal | None = None
    top1_click_share: Decimal | None = None
    top1_conversion_share: Decimal | None = None
    top2_conversion_share: Decimal | None = None
    top3_conversion_share: Decimal | None = None
    traffic_metrics: MetricSnapshot | None = None
    raw: dict[str, str]


class ImportReport(Contract):
    id: UUID
    project_id: UUID
    run_id: UUID
    core_selection_id: UUID
    competitor_id: UUID | None
    provider: Literal["sellersprite"] = "sellersprite"
    metadata: ImportMetadata
    filename: str
    file_sha256: str
    created_at: datetime
    confirmed_at: datetime | None = None
    status: Literal["preview", "confirmed"] = "preview"
    stale: bool = False
    complete: bool = False
    truncated: bool = False
    imported_rows: int
    unique_rows: int = 0
    headers: list[str]
    samples: list[dict[str, str]]
    suggested_mapping: dict[str, str]
    confirmation: ImportConfirmation | None = None
    mapping_version: str = "sellersprite-export-v1"


class ImportWorkspace(Contract):
    reports: list[ImportReport]
    forward_queries: list[str]
    reverse_queries: list[str]
    complete: bool = False
    covered_forward: int = 0
    covered_reverse: int = 0


class ImportPage(Contract):
    total: int
    offset: int
    limit: int
    items: list[ImportRow]
