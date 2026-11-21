"""Read-only, release-scoped access to a frozen keyword library."""

import hashlib
import hmac
import re
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi import Path as PathParameter
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from app.db.repository import Repository
from app.domain.library import LibraryEntry, LibraryReport, Tier

bearer = HTTPBearer(auto_error=False)


class LibraryGrant(BaseModel):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    project_id: UUID
    version: int = Field(ge=1)


_grants = TypeAdapter(list[LibraryGrant])


class PublicKeyword(BaseModel):
    keyword: str
    aba_rank: int | None
    tier: Tier
    priority: Literal["high", "normal", "low"]
    usage: Literal["pending", "listing", "advertising", "both", "paused"]

    @classmethod
    def from_entry(cls, entry: LibraryEntry) -> "PublicKeyword":
        return cls(
            keyword=entry.keyword,
            aba_rank=entry.evidence.metrics.rank,
            tier=entry.tier,
            priority=entry.priority,
            usage=entry.usage,
        )


class PublicReleasePage(BaseModel):
    project_id: UUID
    version: int
    published_at: datetime
    total: int
    next_offset: int | None
    items: list[PublicKeyword]


def build_public_library_router(repository: Repository, keys_file: Path) -> APIRouter:
    router = APIRouter(prefix="/api/public/v1", tags=["published keyword library"])

    @router.get("/libraries/{project_id}/versions/{version}")
    def published_release(
        project_id: UUID,
        version: Annotated[int, PathParameter(ge=1)],
        response: Response,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
    ) -> PublicReleasePage:
        token = credentials.credentials if credentials else ""
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            raise HTTPException(401, "Invalid API key", headers={"WWW-Authenticate": "Bearer"})
        try:
            grants = _grants.validate_json(keys_file.read_bytes())
        except (OSError, ValidationError) as exc:
            raise HTTPException(503, "API access is unavailable") from exc
        sha256 = hashlib.sha256(token.encode("ascii")).hexdigest()
        matching = [grant for grant in grants if hmac.compare_digest(grant.sha256, sha256)]
        if not matching:
            raise HTTPException(401, "Invalid API key", headers={"WWW-Authenticate": "Bearer"})
        if not any(
            grant.project_id == project_id and grant.version == version for grant in matching
        ):
            raise HTTPException(404, "Library release not found")
        with repository.connect() as conn:
            row = conn.execute(
                "SELECT document FROM keyword_library_publications WHERE project_id=? AND version=?",
                (str(project_id), version),
            ).fetchone()
        if row is None:
            raise HTTPException(404, "Library release not found")
        report = LibraryReport.model_validate_json(cast(str, row[0]))
        if (
            report.published_at is None
            or report.project_id != project_id
            or report.publication_version != version
        ):
            raise HTTPException(404, "Library release not found")
        end = offset + limit
        response.headers["Cache-Control"] = "no-store"
        return PublicReleasePage(
            project_id=project_id,
            version=version,
            published_at=report.published_at,
            total=len(report.entries),
            next_offset=end if end < len(report.entries) else None,
            items=[PublicKeyword.from_entry(entry) for entry in report.entries[offset:end]],
        )

    return router
