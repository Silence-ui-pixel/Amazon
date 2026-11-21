"""模型决策契约：只接受给定集合内的选择，输出必须过 schema 校验。"""

from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator

from app.domain.schemas import Asin, Contract


class RootChoice(Contract):
    candidate_id: UUID
    reason: str = Field(min_length=1, max_length=300)


class RootDecision(Contract):
    selected: list[RootChoice] = Field(min_length=1, max_length=5)
    # 逐项理由已足够；顶层理由允许省略，避免仅因缺一句总结就整轮失败。
    reason: str = Field(default="", max_length=300)


class CompetitorJudgment(Contract):
    asin: Asin
    decision: Literal["include", "exclude"]
    reason: str = Field(min_length=1, max_length=300)
    price_override_reason: str = Field(default="", max_length=300)

    @field_validator("price_override_reason", mode="before")
    @classmethod
    def no_override_is_empty(cls, value: object) -> object:
        # 无例外说明时 null 与空串含义一致；是否允许纳入仍由价格审核门校验。
        return "" if value is None else value


class CompetitorDecision(Contract):
    items: list[CompetitorJudgment] = Field(min_length=1, max_length=500)


class CleaningRoot(Contract):
    term: str = Field(min_length=1, max_length=100)
    category: Literal["selling_point", "scene", "preference", "unclassified"]
    # 模型偶尔返回空 evidence；空证据只影响展示文案，不该让整批分类作废。
    evidence: str = Field(default="", max_length=300)


class CleaningExclusion(Contract):
    keyword: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=300)


class CleaningDecision(Contract):
    roots: list[CleaningRoot] = Field(min_length=1, max_length=100)
    exclusions: list[CleaningExclusion] = Field(
        default_factory=list[CleaningExclusion], max_length=100
    )
    reason: str = Field(min_length=1, max_length=500)


class TrafficJudgment(Contract):
    keyword: str = Field(min_length=1, max_length=500)
    decision: Literal["keep", "exclude", "defer"]
    reason: str = Field(min_length=1, max_length=500)
    evidence_note: str = Field(min_length=1, max_length=500)


class TrafficDecision(Contract):
    items: list[TrafficJudgment] = Field(min_length=1, max_length=100)
