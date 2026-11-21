"""Read-only navigation summary and conditional reads of persisted result details.

No task is resumed, created, or written here. PostgreSQL tuple versions avoid reading
large JSON/rows columns just to discover whether a browser copy is still current.
"""

import hashlib
import json
import re
from datetime import date
from pathlib import Path
from typing import Any
from uuid import UUID

from app.db.models import metadata
from app.db.repository import Repository
from app.domain.errors import DomainError
from app.domain.library import LibraryRequest
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

GROUPS = (
    (
        "ensure_input",
        "identify_product",
        "product_confirmation",
        "generate_roots",
        "root_selection",
    ),
    ("week", "collect_core_report", "core_terms"),
    ("competitor_discovery", "competitor_selection"),
    ("submit_sources", "wait_sources"),
    ("third_party",),
    ("create_cleaning", "cleaning_review"),
    ("create_traffic", "traffic_review"),
    ("create_library", "library_publish"),
)
BASE = r"/api/v1/projects/([0-9a-f-]{36})/pipeline-runs/([0-9a-f-]{36})"
# Only persisted result reads: never cache progress, collectors, actions, or exports.
DETAIL = re.compile(
    BASE + r"/(?:library(?:/[0-9a-f-]{36}(?:/template)?)?"
    r"|traffic(?:/[0-9a-f-]{36}/(?:rows|reviews))?"
    r"|cleaning(?:/[0-9a-f-]{36}/rows)?"
    r"|core-selection|competitors|aba-reports(?:/[0-9a-f-]{36})?)$"
)


def version_queries():
    """Cover upstream writes, append-only reviews and deletions, not just max(version)."""
    parts = []
    for table in metadata.sorted_tables:
        if table.name in {
            "workflow_runs",
            "browser_operations",
            "research_record_deletions",
        }:
            continue
        if "run_id" in table.c:
            where = "t.run_id=:run"
        elif table.name == "pipeline_runs":
            where = "t.id=:run"
        elif table.name in {"projects", "images", "keyword_library_publications"}:
            where = (
                "t.id" if table.name == "projects" else "t.project_id"
            ) + "=:project"
        elif "analysis_id" in table.c:
            fk = next(iter(table.c.analysis_id.foreign_keys), None)
            if fk is None:
                continue
            parent = fk.column.table.name
            where = f"t.analysis_id IN (SELECT id FROM {parent} WHERE run_id=:run)"
        elif table.name == "rule_sets":
            where = "TRUE"
        else:
            continue
        parts.append(
            f"SELECT '{table.name}' AS name, count(*) AS count, "
            f"md5(coalesce(string_agg(t.xmin::text || ':' || t.ctid::text, ',' "
            f"ORDER BY t.ctid), '')) AS version FROM {table.name} t WHERE {where}"
        )
    return " UNION ALL ".join(parts)


VERSION_SQL = version_queries()
SOURCE_VERSION = hashlib.sha256(
    b"".join(
        p.read_bytes()
        for p in sorted(Path(__file__).resolve().parents[2].rglob("*.py"))
    )
).hexdigest()


def result_version(repository: Repository, project: UUID, run: UUID) -> str:
    from sqlalchemy import text

    with repository.engine.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pipeline_runs WHERE id=:run AND project_id=:project"),
            {"run": str(run), "project": str(project)},
        ).first()
        if not exists:
            return ""
        rows = conn.execute(
            text(VERSION_SQL), {"run": str(run), "project": str(project)}
        ).all()
    # Model configuration can affect freshness without writing a business table.
    configs = [
        (
            str(p.relative_to(repository.storage_root)),
            p.stat().st_mtime_ns,
            p.stat().st_size,
        )
        for p in repository.storage_root.glob("*.json")
        if p.is_file()
    ]
    payload = json.dumps(
        ["result-loading-v1", SOURCE_VERSION, [tuple(r) for r in rows], sorted(configs)]
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def step_states(view: dict | None) -> list[str]:
    nodes = {n["key"]: n["status"] for n in (view or {}).get("nodes", [])}
    states = []
    for group in GROUPS:
        values = [nodes.get(key, "pending") for key in group]
        if all(v in ("done", "skipped") for v in values):
            states.append("done")
        elif any(v in ("running", "waiting_external") for v in values):
            states.append(
                "running"
                if (view or {}).get("status")
                in ("queued", "running", "waiting_external", "waiting_human")
                else "review"
            )
        elif any(
            v in ("waiting_human", "failed", "interrupted", "done") for v in values
        ):
            states.append("review")
        else:
            states.append("waiting")
    return states


def manual_steps(
    repository: Repository,
    services: dict[str, Any],
    project: UUID,
    run: UUID,
    week: str | None,
    root_selection: dict | None,
) -> list[str]:
    """Old/manual runs have no graph projection. Reuse existing freshness checks.

    These are read-only services, and only eight statuses leave the server. In
    particular, don't load the library's frozen evidence just to inspect publication.
    """

    def optional(read):
        try:
            return read()
        except DomainError as exc:
            if exc.status_code not in (404, 409, 422):
                raise
            return None

    states = ["waiting"] * 8
    states[0] = "done" if (root_selection or {}).get("candidate_ids") else "waiting"
    core = optional(lambda: services["core"].selection(project, run))
    states[1] = "done" if core and not core.stale else "review" if core else "waiting"
    competitors = optional(lambda: services["competitors"].get(project, run))
    states[2] = (
        "done"
        if competitors
        and competitors.confirmed_at
        and not competitors.stale
        and states[1] == "done"
        else "review"
        if competitors
        else "waiting"
    )
    for index, key, arg in [
        (3, "expansion", date.fromisoformat(week) if week else None),
        (4, "third_party", None),
    ]:
        value = optional(
            lambda key=key, arg=arg: services[key].workspace(project, run, arg)
        )
        states[index] = (
            "done"
            if value and value.complete and states[1:3] == ["done", "done"]
            else "review"
            if value and value.reports
            else "waiting"
        )
        if value and any(getattr(r, "status", "") == "running" for r in value.reports):
            states[index] = "running"
    cleanings = optional(lambda: services["cleaning"].history(project, run)) or []
    cleaning = cleanings[0] if cleanings else None
    states[5] = (
        "done"
        if cleaning
        and cleaning.review
        and not cleaning.stale
        and (not week or str(cleaning.request.aba_week_end) == week)
        and cleaning.request.seller_period == "last_30_days"
        and states[1:5] == ["done"] * 4
        else "review"
        if cleaning
        else "waiting"
    )
    traffics = optional(lambda: services["traffic"].history(project, run)) or []
    traffic = traffics[0] if traffics else None
    states[6] = (
        "done"
        if traffic and traffic.complete and not traffic.stale
        else "review"
        if traffic
        else "waiting"
    )
    with repository.connect() as conn:
        libraries = conn.execute(
            "SELECT CAST(document AS jsonb) -> 'request', "
            "CAST(document AS jsonb) ->> 'publication_version' FROM keyword_library_drafts "
            "WHERE run_id=? ORDER BY seq DESC",
            (str(run),),
        ).fetchall()
    for index, row in enumerate(libraries):
        data = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        try:
            services["library"]._fresh(
                project, run, LibraryRequest.model_validate(data)
            )
        except (DomainError, ValueError):
            continue
        if index == 0:
            states[7] = "review"
        if row[1] is not None and states[6] == "done":
            states[7] = "done"
            break
    return states


def build_result_loading_router(
    repository: Repository, services: dict[str, Any]
) -> APIRouter:
    router = APIRouter(tags=["result-loading"])

    @router.get("/api/v1/projects/{project_id}/pipeline-runs/{run_id}/summary")
    def summary(project_id: UUID, run_id: UUID):
        repository.get_run(project_id, run_id)
        with repository.connect() as conn:
            row = conn.execute(
                "SELECT (CAST(view AS jsonb) - 'events')::text, "
                "CAST(state AS jsonb) ->> 'aba_week_end' FROM workflow_runs "
                "WHERE project_id=? AND run_id=? ORDER BY created_at DESC, seq DESC LIMIT 1",
                (str(project_id), str(run_id)),
            ).fetchone()
            core = conn.execute(
                "SELECT id FROM core_selections WHERE run_id=? ORDER BY version DESC LIMIT 1",
                (str(run_id),),
            ).fetchone()
            competitors = conn.execute(
                "SELECT id FROM competitor_versions WHERE run_id=? ORDER BY version DESC LIMIT 1",
                (str(run_id),),
            ).fetchone()
            roots = conn.execute(
                "SELECT document FROM root_selections WHERE run_id=?", (str(run_id),)
            ).fetchone()
            week_row = conn.execute(
                "SELECT CAST(document AS jsonb) ->> 'week_end' FROM aba_reports "
                "WHERE run_id=? ORDER BY created_at DESC, seq DESC LIMIT 1",
                (str(run_id),),
            ).fetchone()
        view = json.loads(row[0]) if row and row[0] else None
        week = (row[1] if row else None) or (week_row[0] if week_row else None)
        root_selection = json.loads(roots[0]) if roots else None
        if view is not None:
            view["events"] = []
        states = (
            step_states(view)
            if view
            else manual_steps(
                repository, services, project_id, run_id, week, root_selection
            )
        )
        return {
            "run_id": str(run_id),
            "workflow": view,
            "steps": states,
            "report_week": week,
            "core_selection_id": core[0] if core else "",
            "competitor_selection_id": competitors[0] if competitors else "",
            "root_selection": root_selection,
            "result_version": result_version(repository, project_id, run_id),
        }

    return router


def install_result_cache(application: FastAPI, repository: Repository) -> None:
    @application.middleware("http")
    async def conditional_result(request: Request, call_next):
        match = DETAIL.fullmatch(request.url.path) if request.method == "GET" else None
        if not match:
            return await call_next(request)
        try:
            project, run = map(UUID, match.groups())
        except ValueError:
            return await call_next(request)
        before = await run_in_threadpool(result_version, repository, project, run)
        # The URL includes pagination/filter parameters; a validator belongs to that URL.
        etag = (
            '"' + hashlib.sha256((str(request.url) + before).encode()).hexdigest() + '"'
        )
        if before and request.headers.get("if-none-match") == etag:
            return Response(
                status_code=304,
                headers={"ETag": etag, "Cache-Control": "private, no-cache"},
            )
        response = await call_next(request)
        if before and response.status_code == 200:
            after = await run_in_threadpool(result_version, repository, project, run)
            if before == after:
                response.headers["ETag"] = etag
                response.headers["Cache-Control"] = "private, no-cache"
        return response
