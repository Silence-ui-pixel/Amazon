"""第二步 v2：按商品词持久化候选采集与非阻塞表格同步状态。"""

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from app.domain.schemas import Contract

CollectionStatus = Literal["queued", "collecting", "completed", "failed"]
ExportStatus = Literal[
    "queued",
    "downloading",
    "parsing",
    "verifying",
    "completed",
    "download_failed",
    "parse_failed",
    "verification_failed",
]


class HotSearchTermTask(Contract):
    product_term: str
    status: CollectionStatus = "queued"
    page: int = 0
    candidate_count: int = 0
    last_output: str = "等待采集"
    error_code: str = ""
    error: str = ""
    export_status: ExportStatus = "queued"
    export_error_code: str = ""
    export_error: str = ""
    retries: int = 0
    updated_at: datetime


class HotSearchCollectionJob(Contract):
    id: UUID
    project_id: UUID
    run_id: UUID
    fingerprint: str
    context_hash: str
    rule_version: Literal["hot-search-core-v2"] = "hot-search-core-v2"
    week_start: date
    week_end: date
    status: CollectionStatus = "queued"
    report_id: UUID | None = None
    tasks: list[HotSearchTermTask] = Field(min_length=1)
    created_at: datetime
    updated_at: datetime
    stale: bool = False
