"""第七步确定性规则；比例用 Decimal 比较，显示舍入不参与判定。"""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_serializer,
    model_validator,
)

from app.domain.cleaning import Phrase
from app.domain.schemas import Contract, Name


class RuleSet(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str = "sop-traffic-v3"
    purchase_threshold: Decimal = Decimal("0.60")
    monopoly_first: Decimal = Decimal("0.25")
    monopoly_three: Decimal = Decimal("0.35")
    price_delta_max: Decimal = Decimal("0.15")
    result_share_min: Decimal = Decimal("0.50")
    deep_rank_max: int = 150000


class MetricInput(BaseModel):
    rank: int | None = None
    click: Decimal | None = None
    conversion: Decimal | None = None
    conversion2: Decimal | None = None
    conversion3: Decimal | None = None


class Metrics(BaseModel):
    first_click_share: Decimal | None = None
    first_conversion_share: Decimal | None = None
    rank: int | None = None
    purchase_ratio: Decimal | None = None
    purchase: bool | None = None
    monopoly: bool | None = None
    conversion_sum3: Decimal | None = None
    deep_analysis: bool = False
    missing: list[str] = Field(default_factory=list[str])


class MetricSnapshot(BaseModel):
    """来源行在采集/导入阶段生成的确定性指标及其规则版本。"""

    model_config = ConfigDict(frozen=True)
    rule_id: str
    metrics: Metrics

    @field_serializer("metrics", when_used="always")
    def serialize_metrics(self, value: Metrics) -> dict[str, object]:
        """保持来源行原有的 model_dump → json.dumps 序列化契约。"""
        return value.model_dump(mode="json")


def evaluate(data: MetricInput, rules: RuleSet) -> Metrics:
    result = Metrics(
        rank=data.rank,
        deep_analysis=data.rank is not None and data.rank <= rules.deep_rank_max,
        first_click_share=data.click,
        first_conversion_share=data.conversion,
    )
    if data.rank is None:
        result.missing.append("缺少所选周 ABA 排名")
    if data.click is None or data.click <= 0 or data.conversion is None:
        result.missing.append("首位点击份额缺失或为零，或同一产品转化份额缺失")
    else:
        # v3 与来源表统一为严格 >60%；v2 历史快照保留其 >=60% 口径。
        result.purchase_ratio = data.conversion / data.click
        result.purchase = (
            result.purchase_ratio >= rules.purchase_threshold
            if rules.id == "sop-traffic-v2"
            else result.purchase_ratio > rules.purchase_threshold
        )
    shares = [data.conversion, data.conversion2, data.conversion3]
    # 前三份额缺失时不能当作 0；v2 的首位 >25% 已足够判定垄断，无需其余两位。
    if any(s is None for s in shares):
        result.missing.append("前三产品转化份额不完整")
        if (
            rules.id != "sop-traffic-v1"
            and data.conversion is not None
            and data.conversion > rules.monopoly_first
        ):
            result.monopoly = True
    else:
        result.conversion_sum3 = sum((s for s in shares if s is not None), Decimal(0))
        result.monopoly = (
            data.conversion is not None and data.conversion > rules.monopoly_first
        ) or result.conversion_sum3 > rules.monopoly_three
    return result


def metric_snapshot(data: MetricInput, rules: RuleSet | None = None) -> MetricSnapshot:
    """使用当前或指定规则生成可随来源行持久化的指标快照。"""
    selected = rules or RuleSet()
    return MetricSnapshot(rule_id=selected.id, metrics=evaluate(data, selected))


class RelevanceEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    style_similar: bool | None = None
    price_delta_ratio: Decimal | None = Field(default=None, ge=0, le=1)
    first_half_result_share: Decimal | None = Field(default=None, ge=0, le=1)


# 三项证据必须齐全：缺项为 unknown，不能把未核实误写成不相关或高相关。
def relevance(evidence: RelevanceEvidence, rules: RuleSet) -> Literal["high", "low", "unknown"]:
    if (
        evidence.style_similar is None
        or evidence.price_delta_ratio is None
        or evidence.first_half_result_share is None
    ):
        return "unknown"
    return (
        "high"
        if (
            evidence.style_similar
            and evidence.price_delta_ratio <= rules.price_delta_max
            and evidence.first_half_result_share >= rules.result_share_min
        )
        else "low"
    )


class TrafficRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cleaning_id: UUID
    cleaning_version: int = Field(ge=1)


class TrafficReview(RelevanceEvidence):
    expected_version: int = Field(ge=0)
    keyword: str = Field(min_length=1, max_length=500)
    decision: Literal["keep", "exclude", "defer"]
    priority: Literal["high", "normal", "low"]
    reason: str = Field(min_length=1, max_length=1000)
    evidence_note: str = Field(min_length=1, max_length=2000)
    reviewer: str = Field(min_length=1, max_length=100)


class ReviewRecord(BaseModel):
    version: int
    created_at: datetime
    data: TrafficReview
    relevance: Literal["high", "low", "unknown"]


class BatchTrafficReview(Contract):
    expected_version: int = Field(ge=0)
    keywords: list[str] = Field(min_length=1, max_length=100)
    decision: Literal["keep", "exclude", "defer"]
    priority: Literal["high", "normal", "low"]
    reason: Name
    reviewer: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    evidence_note: Name

    @model_validator(mode="after")
    def unique_keywords(self) -> "BatchTrafficReview":
        if len(set(self.keywords)) != len(self.keywords):
            raise ValueError("批量审核不能重复选择关键词")
        return self


class Reference(BaseModel):
    """第三方词表原值；仅供人工参考，不参与指标计算与层级划分。"""

    provider: Literal["sellersprite"] = "sellersprite"
    direction: Literal["forward", "reverse"]
    query: str
    period_start: date | None = None
    period_end: date | None = None
    period_kind: Literal["date_range", "last_30_days"] = "date_range"
    collected_at: datetime | None = None
    weekly_rank: int | None = None
    monthly_searches: int | None = None


class SourceMetricRow(BaseModel):
    """第七步来源行计算；不跨来源合并点击或转化份额。"""

    source_id: UUID
    provider: Literal["aba", "sellersprite"]
    direction: Literal["forward", "reverse"]
    query: str
    source_row: int
    original: str
    period_start: date | None = None
    period_end: date | None = None
    period_kind: Literal["date_range", "last_30_days"] = "date_range"
    collected_at: datetime | None = None
    metrics: Metrics


class TrafficRow(BaseModel):
    phrase: Phrase
    metrics: Metrics
    cleaning_exclusion: str = ""
    references: list[Reference] = Field(default_factory=list[Reference])
    source_metrics: list[SourceMetricRow] = Field(default_factory=list[SourceMetricRow])
    review: ReviewRecord | None = None


def sop_advice(row: TrafficRow) -> str:
    """缺证据不臆测相关性；只给分析顺序建议，不修改人工保留决定。"""
    if row.review is None or row.review.relevance == "unknown":
        return "待核对相关性"
    if row.review.relevance == "low":
        return "低相关，复核后决定"
    if row.metrics.monopoly is True:
        return "后投（垄断词）"
    if row.metrics.purchase is False:
        return "后投（购买比不足）"
    if row.metrics.purchase is None or row.metrics.monopoly is None:
        return "待补ABA指标"
    return "先投"


class TrafficReport(BaseModel):
    id: UUID
    project_id: UUID
    run_id: UUID
    created_at: datetime
    request: TrafficRequest
    rules: RuleSet
    total: int
    required: int
    reviewed: int = 0
    version: int = 0
    complete: bool = False
    stale: bool = False


class TrafficPage(BaseModel):
    total: int
    offset: int
    limit: int
    items: list[TrafficRow]


class TrafficReviewProgress(BaseModel):
    """进度使用已入库审核数；任务状态跨刷新、重启保留。"""
    analysis_id: UUID
    project_id: UUID
    run_id: UUID
    status: Literal["idle", "queued", "running", "succeeded", "failed"] = "idle"
    attempt: int = 0
    total: int = 0
    required: int = 0
    reviewed: int = 0
    version: int = 0
    complete: bool = False
    stale: bool = False
    error_code: str = ""
    error: str = ""
    updated_at: datetime
