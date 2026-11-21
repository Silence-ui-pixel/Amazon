"""词根候选及人工选择契约；来源与人工确认保持分离。"""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, StringConstraints, model_validator

from app.domain.schemas import Contract, Marketplace
from app.domain.text import contains_root, fold_text

Term = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Explanation = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
# 当前仅支持美国站；保留站点到语言的映射，便于后续扩展时按站点取语言。
LANGUAGES: dict[str, str] = {"US": "en"}

# LLM 输出的字段名会漂移（如 reason 写成 description）；解析侧做别名归一与多余字段忽略，
# 长度、类型等硬约束不变。序列化仍只输出正式字段。
_DRAFT_ALIASES: dict[str, tuple[str, ...]] = {
    "text": ("text", "keyword", "term", "name"),
    "translation": ("translation", "chinese", "translation_zh"),
    "reason": ("reason", "description", "why", "rationale", "justification"),
}
_SET_ALIASES: dict[str, tuple[str, ...]] = {
    "candidates": ("candidates", "items", "roots", "words"),
}


def _alias_first(data: dict, aliases: dict[str, tuple[str, ...]]) -> dict:
    fixed = dict(data)
    for target, names in aliases.items():
        if fixed.get(target):
            continue
        for name in names:
            value = fixed.get(name)
            if target == "candidates":
                if isinstance(value, list) and value:
                    fixed[target] = value
                    break
            elif isinstance(value, str) and value.strip():
                fixed[target] = value
                break
    return fixed


class RootDraft(Contract):
    model_config = ConfigDict(extra="ignore")

    text: Term
    translation: Explanation
    reason: Explanation
    # 拓词仅用于搜索取证；text 始终是最终入库的最小商品词根。
    expansions: list[Term] = Field(default_factory=list, max_length=5)

    @model_validator(mode="before")
    @classmethod
    def tolerate_field_drift(cls, data: object) -> object:
        return _alias_first(data, _DRAFT_ALIASES) if isinstance(data, dict) else data

    @model_validator(mode="after")
    def normalize_expansions(self) -> "RootDraft":
        seen = {fold_text(self.text)}
        queries: list[str] = []
        for query in self.expansions:
            # 拉丁语言按完整词包含检查，避免 wheel 误关联到 wheelbarrow。
            if not contains_root(query, self.text):
                raise ValueError("扩展搜索词必须包含完整的最小词根")
            key = fold_text(query)
            if key not in seen:
                seen.add(key)
                queries.append(query)
        self.expansions = queries
        return self


class RootDraftSet(Contract):
    model_config = ConfigDict(extra="ignore")

    candidates: list[RootDraft] = Field(min_length=1, max_length=10)
    validation_issues: list["CandidateValidationIssue"] = Field(default_factory=list)
    raw_response: str = ""
    diagnostic_id: str = ""

    @model_validator(mode="before")
    @classmethod
    def tolerate_field_drift(cls, data: object) -> object:
        return _alias_first(data, _SET_ALIASES) if isinstance(data, dict) else data

    @model_validator(mode="after")
    def remove_duplicates(self) -> "RootDraftSet":
        # 大小写、全半角和多余空白不应产生重复的人工选择项。
        seen: set[str] = set()
        unique: list[RootDraft] = []
        for candidate in self.candidates:
            key = fold_text(candidate.text)
            if key not in seen:
                seen.add(key)
                unique.append(candidate)
        self.candidates = unique
        return self


class RootCandidate(RootDraft):
    id: UUID


class CandidateValidationIssue(Contract):
    text: str
    code: str
    message: str


class RootCandidateVersion(Contract):
    project_id: UUID
    run_id: UUID
    # 新记录标明实际供应商；bailian 仅用于兼容切换前已持久化的结果。
    provider: Literal["mimo", "bailian"] = "mimo"
    model: str
    marketplace: Marketplace
    language: str
    created_at: datetime
    candidates: list[RootCandidate] = Field(default_factory=list, max_length=10)
    warnings: list[str]
    # 无字段的历史 JSON 属于旧词根规则；新生成记录显式写入 product-term-v1。
    rule_version: Literal["legacy-root-v1", "product-term-v1"] = "legacy-root-v1"
    version: int = Field(default=1, ge=1)
    source: Literal["model", "manual"] = "model"
    validation_issues: list[CandidateValidationIssue] = Field(default_factory=list)
    raw_response: str = ""
    diagnostic_id: str = ""


class RootCandidates(RootCandidateVersion):
    history: list[RootCandidateVersion] = Field(default_factory=list)


class RootCandidatesEdit(Contract):
    expected_version: int = Field(ge=1)
    candidates: list[RootDraft] = Field(min_length=1, max_length=10)


class RootCandidatesRegenerate(Contract):
    expected_version: int = Field(ge=1)


class RootSelectionInput(Contract):
    candidate_ids: list[UUID] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def unique_ids(self) -> "RootSelectionInput":
        if len(self.candidate_ids) != len(set(self.candidate_ids)):
            raise ValueError("不能重复选择同一商品词")
        return self


class RootSelection(RootSelectionInput):
    project_id: UUID
    run_id: UUID
    confirmed_at: datetime
    source: Literal["manual", "automatic"] = "manual"
    verification_id: UUID | None = None
