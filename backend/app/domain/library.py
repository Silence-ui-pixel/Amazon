"""第八步契约与确定性分层。"""

from datetime import datetime
from typing import Literal, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.schemas import Asin, Contract, Name
from app.domain.traffic import TrafficRow

Tier = Literal["L1", "L2", "L3", "L4", "unranked"]


class LibraryRules(BaseModel):
    """分层规则；随快照发布，调整边界需要新版本而不是改历史。"""

    model_config = ConfigDict(frozen=True)
    id: str = "sop-library-v1"
    l1_max: int = 30000
    l2_max: int = 150000
    l3_max: int = 300000
    asin_positions: int = 3
    suggestions_per_tier: int = 30

    @model_validator(mode="after")
    def ordered(self) -> "LibraryRules":
        if not 0 < self.l1_max < self.l2_max < self.l3_max:
            raise ValueError("层级边界必须递增且为正数")
        if self.asin_positions < 1 or self.suggestions_per_tier < 1:
            raise ValueError("ASIN 证据位数与每层建议数必须为正整数")
        return self


# 使用所选周官方 ABA 排名分层；缺失/非正数进入 unranked，第三方排名不能补位。
def tier_of(rank: int | None, rules: LibraryRules) -> Tier:
    if rank is None or rank < 1:
        return "unranked"
    if rank <= rules.l1_max:
        return "L1"
    if rank <= rules.l2_max:
        return "L2"
    return "L3" if rank <= rules.l3_max else "L4"


class LibraryRequest(Contract):
    traffic_id: UUID
    traffic_version: int = Field(ge=1)
    revision_of: UUID | None = None


class EntryEdit(Contract):
    expected_version: int = Field(ge=0)
    keyword: str = Field(min_length=1, max_length=500)
    priority: Literal["high", "normal", "low"]
    usage: Literal["pending", "listing", "advertising", "both", "paused"]
    auxiliary_asins: list[Asin] = Field(max_length=30)
    reason: Name
    operator: Name


class PublishRequest(Contract):
    expected_version: int = Field(ge=0)
    publisher: Name
    reason: Name
    confirmed: Literal[True]

    @model_validator(mode="before")
    @classmethod
    def discard_legacy_exception_reviews(cls, value: object) -> object:
        """旧客户端字段可继续提交，但不再进入发布记录。"""
        if isinstance(value, dict):
            mapping = cast(dict[str, object], value)
            if "exception_reviews" not in mapping:
                return mapping
            cleaned: dict[str, object] = dict(mapping)
            cleaned.pop("exception_reviews", None)
            return cleaned
        return value


class LibraryEntry(Contract):
    keyword: str
    issues: list[str] = Field(default_factory=list[str])
    tier: Tier
    roots: dict[str, list[str]]
    priority: Literal["high", "normal", "low"]
    usage: Literal["pending", "listing", "advertising", "both", "paused"] = "pending"
    auxiliary_asins: list[Asin] = Field(default_factory=list[Asin])
    evidence: TrafficRow


def entry_issues(entry: LibraryEntry) -> list[str]:
    issues = list(entry.evidence.metrics.missing)
    if entry.evidence.metrics.rank is None:
        issues.append("未分层：缺少有效 ABA 周排名")
    if entry.evidence.review is None or entry.evidence.review.relevance == "unknown":
        issues.append("相关性证据待补充")
    elif entry.evidence.review.relevance == "low":
        issues.append("相关性低但人工保留")
    return list(dict.fromkeys(issues))


class AsinSuggestion(Contract):
    tier: Tier
    asin: Asin
    keyword_count: int


class EditRecord(Contract):
    created_at: datetime
    version: int
    data: EntryEdit


class LibraryReport(Contract):
    id: UUID
    project_id: UUID
    run_id: UUID
    created_at: datetime
    request: LibraryRequest
    version: int = 0
    publication_version: int | None = None
    published_at: datetime | None = None
    publication: PublishRequest | None = None
    stale: bool = False
    rules: LibraryRules = Field(default_factory=LibraryRules)
    source_sha256: str
    snapshots: dict[str, str]
    entries: list[LibraryEntry]
    suggestions: list[AsinSuggestion]
    edits: list[EditRecord] = Field(default_factory=list[EditRecord])

    @field_validator("rules", mode="before")
    @classmethod
    def legacy_rules(cls, value: object) -> object:
        # 旧快照把规则存成一句描述字符串；读取时按当前默认规则还原。
        if isinstance(value, str):
            return LibraryRules()
        return value
