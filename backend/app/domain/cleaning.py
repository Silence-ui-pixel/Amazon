"""第六步确定性文本统计；分类词表见 app.domain.taxonomy。"""

import re
from collections import Counter
from datetime import date, datetime
from typing import Literal, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain import taxonomy
from app.domain.text import phrase_key

ALGORITHM = "english-single-root-v2"
# 分类规则版本参与清洗快照指纹；词表或判定顺序变化必须升版，否则旧快照会被复用。
DICTIONARY = "root-taxonomy-v4"
STOP = frozenset(
    [
        "a",
        "an",
        "the",
        "for",
        "with",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "at",
        "by",
        "from",
        "is",
        "are",
        "be",
        "as",
    ]
)
Category = Literal["selling_point", "scene", "preference", "unclassified"]


class Frequency(BaseModel):
    term: str
    occurrences: int
    phrase_count: int
    density: float
    suggestions: list[Category]


def analyze(phrases: list[str]) -> tuple[list[Frequency], int]:
    counts: Counter[str] = Counter()
    coverage: Counter[str] = Counter()
    total = 0
    for original in phrases:
        phrase = phrase_key(original)
        tokens = re.findall(r"[a-z]+|[^a-z\s]+", phrase)
        total += sum(bool(re.fullmatch(r"[a-z]+", t)) for t in tokens)
        terms: list[str] = []
        for token in tokens:
            if not token.isascii() or not token.isalpha() or token in STOP:
                continue
            terms.append(token)
        counts.update(terms)
        coverage.update(set(terms))
    output: list[Frequency] = []
    for term, count in counts.items():
        # 词典只决定提示词里的建议类别；最终归类由服务层规则优先、模型兜底。
        suggestions = [item for item in taxonomy.suggest(term) if item in ("selling_point", "scene", "preference")]
        output.append(
            Frequency(
                term=term,
                occurrences=count,
                phrase_count=coverage[term],
                density=count / total * 100 if total else 0,
                suggestions=cast(list[Category], suggestions),
            )
        )
    return sorted(output, key=lambda x: (-x.occurrences, x.term)), total


class CleaningRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    aba_week_end: date
    seller_period: Literal["date_range", "last_30_days"] = "date_range"
    seller_start: date | None = None
    seller_end: date | None = None
    text_only_acknowledged: Literal[True]

    @model_validator(mode="after")
    def dates(self) -> "CleaningRequest":
        if self.seller_period == "last_30_days":
            if self.seller_start is not None or self.seller_end is not None:
                raise ValueError("最近30天不能填写固定起止日期")
            return self
        if self.seller_start is None or self.seller_end is None:
            raise ValueError("固定周期须填写起止日期")
        if self.seller_end < self.seller_start:
            raise ValueError("结束日期不能早于开始日期")
        # 三方仅合并文本，固定周期历史证据无需覆盖 ABA 报告周。
        return self


class Source(BaseModel):
    id: UUID
    provider: Literal["aba", "sellersprite"]
    direction: Literal["forward", "reverse"]
    query: str
    period_start: date | None = None
    period_end: date | None = None
    period_kind: Literal["date_range", "last_30_days"] = "date_range"
    collected_at: datetime | None = None
    truncated: bool = False


class Evidence(BaseModel):
    source_id: UUID
    query: str
    original: str
    source_row: int
    payload: str  # 完整来源行 JSON，数值与缺失值不改写。


class Phrase(BaseModel):
    keyword: str
    evidence: list[Evidence]


class RootReview(BaseModel):
    term: str = Field(min_length=1, max_length=200)
    category: Category
    evidence: str = Field(default="历史人工分类", min_length=1, max_length=300)


class Exclusion(BaseModel):
    keyword: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=500)


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=0)
    roots: list[RootReview] = Field(max_length=5000)
    exclusions: list[Exclusion] = Field(default_factory=lambda: list[Exclusion](), max_length=10000)
    reason: str = Field(min_length=1, max_length=1000)


class CleaningReport(BaseModel):
    id: UUID
    project_id: UUID
    run_id: UUID
    created_at: datetime
    request: CleaningRequest
    sources: list[Source]
    algorithm_version: str = ALGORITHM
    dictionary_version: str = DICTIONARY
    phrase_count: int
    token_count: int
    frequency_count: int
    version: int = 0
    review: Review | None = None
    stale: bool = False


class CleaningPage(BaseModel):
    total: int
    offset: int
    limit: int
    phrases: list[Phrase] = Field(default_factory=lambda: list[Phrase]())
    frequencies: list[Frequency] = Field(default_factory=lambda: list[Frequency]())
