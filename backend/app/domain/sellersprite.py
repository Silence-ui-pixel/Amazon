"""卖家精灵采集使用供应商最近30天窗口；历史月任务仍可读取。"""

from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from app.domain.schemas import Contract

CollectionMethod = Literal["keyword_mining", "keyword_reverse", "traffic_extension"]
VariantScope = Literal["all", "best_selling", "current"]


class BrowserCollectionInput(Contract):
    direction: Literal["forward", "reverse"]
    query: str = Field(default="", max_length=300)
    method: CollectionMethod | None = None
    queries: list[str] = Field(default_factory=list, max_length=20)
    variant_scope: VariantScope | None = None
    period: Literal["last_30_days"] = "last_30_days"

    @model_validator(mode="after")
    def validate_method(self) -> Self:
        method = self.method or (
            "keyword_mining" if self.direction == "forward" else "keyword_reverse"
        )
        expected_direction = "forward" if method == "keyword_mining" else "reverse"
        if self.direction != expected_direction:
            raise ValueError("采集方式与正向/反向方向不一致")
        if method == "traffic_extension":
            if not 1 <= len(self.queries) <= 20:
                raise ValueError("拓展流量词须选择 1 至 20 个竞对 ASIN")
            normalized = [value.strip().upper() for value in self.queries]
            if any(len(value) != 10 or not value.isalnum() for value in normalized):
                raise ValueError("竞对 ASIN 格式不正确")
            if len(set(normalized)) != len(normalized):
                raise ValueError("拓展流量词不能重复选择竞对 ASIN")
            if self.variant_scope is None:
                raise ValueError("拓展流量词须选择变体范围")
            self.queries = normalized
            self.query = ",".join(normalized)
        else:
            query = self.query.strip()
            if not query:
                raise ValueError("查询对象不能为空")
            if self.variant_scope is not None:
                raise ValueError("变体范围只用于多 ASIN 拓展流量词")
            if self.queries and (len(self.queries) != 1 or self.queries[0].strip() != query):
                raise ValueError("单查询采集只能包含当前查询对象")
            self.query = query
            self.queries = [query]
        self.method = method
        return self


class LegacyMonthlyCollectionInput(Contract):
    direction: Literal["forward", "reverse"]
    query: str
    month: str


class BrowserCollectionJob(Contract):
    id: UUID
    project_id: UUID
    run_id: UUID
    request: BrowserCollectionInput | LegacyMonthlyCollectionInput
    core_selection_id: UUID
    competitor_id: UUID | None
    status: Literal["queued", "running", "succeeded", "failed"] = "queued"
    stage: str = "排队中"
    error: str = ""
    error_code: str = ""
    report_id: UUID | None = None
    created_at: datetime
    updated_at: datetime
