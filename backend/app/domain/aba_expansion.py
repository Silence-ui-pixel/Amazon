"""第四步周报采集与正反来源契约。"""

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from app.domain.aba import AbaProduct, AbaRequest
from app.domain.schemas import Contract

Direction = Literal["forward", "reverse"]


class ExpansionRequest(AbaRequest):
    direction: Direction


class ExpansionQuery(Contract):
    query: str
    # legacy 仅用于读取旧报告；新任务会显式从 pending 走到终态。
    status: Literal["legacy", "pending", "running", "succeeded", "failed", "interrupted"] = (
        "legacy"
    )
    total: int = 0
    fetched: int = 0
    pages: int = 0
    truncated: bool = False
    stop_reason: str = ""
    error_code: str | None = None
    error: str | None = None


class ExpansionReport(Contract):
    id: UUID
    project_id: UUID
    run_id: UUID
    core_selection_id: UUID
    competitor_id: UUID | None = None
    direction: Direction
    week_start: date
    week_end: date
    created_at: datetime
    finished_at: datetime | None = None
    max_rows: int
    protocol_version: str = "aba-expansion-v1"
    status: Literal["running", "succeeded", "partial", "failed", "interrupted"] = "running"
    stale: bool = False
    queries: list[ExpansionQuery] = Field(default_factory=list[ExpansionQuery])
    warnings: list[str] = Field(default_factory=list[str])


class ExpansionWorkspace(Contract):
    reports: list[ExpansionReport]
    complete: bool = False
    comparison: dict[str, int] = Field(default_factory=dict[str, int])


class ExpansionEvidence(Contract):
    query: str
    keyword: str
    search_frequency_rank: int
    products: list[AbaProduct]


class ExpansionKeyword(Contract):
    keyword: str
    search_frequency_rank: int | None
    rank_conflict: bool
    also_in_other_direction: bool = False
    evidence: list[ExpansionEvidence]


class ExpansionPage(Contract):
    total: int
    offset: int
    limit: int
    items: list[ExpansionKeyword]
