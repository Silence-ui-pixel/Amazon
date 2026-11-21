"""第八步草稿与发布接口。"""

from uuid import UUID

from fastapi import APIRouter
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.adapters.library_template import TemplateData, export_xlsx, template_data
from app.domain.library import EntryEdit, LibraryReport, LibraryRequest, PublishRequest
from app.domain.library_relevance import (
    RelevanceExport,
    RelevanceExportLookup,
    RelevanceExportRequest,
)
from app.services.library import LibraryService
from app.services.library_relevance import LibraryRelevanceService


class RefreshAttachmentsRequest(BaseModel):
    expected_version: int = Field(ge=0)


def build_library_router(
    service: LibraryService, relevance: LibraryRelevanceService | None = None,
) -> APIRouter:
    router = APIRouter(tags=["library"])
    base = "/api/v1/projects/{project_id}/pipeline-runs/{run_id}/library"

    if relevance is not None:
        @router.post(base + "/{key}/relevance-exports")
        def start_relevance(
            project_id: UUID, run_id: UUID, key: UUID, data: RelevanceExportRequest,
        ) -> RelevanceExport:
            return relevance.ensure(project_id, run_id, key, data.ranked_only)

        @router.get(base + "/{key}/relevance-exports/latest")
        def latest_relevance(
            project_id: UUID, run_id: UUID, key: UUID, ranked_only: bool = True,
        ) -> RelevanceExportLookup:
            return RelevanceExportLookup(job=relevance.latest(project_id, run_id, key, ranked_only))

        @router.get(base + "/{key}/relevance-exports/{job_id}")
        def get_relevance(project_id: UUID, run_id: UUID, key: UUID, job_id: UUID) -> RelevanceExport:
            return relevance.get(project_id, run_id, key, job_id)

        @router.post(base + "/{key}/relevance-exports/{job_id}/retry")
        def retry_relevance(project_id: UUID, run_id: UUID, key: UUID, job_id: UUID) -> RelevanceExport:
            return relevance.retry(project_id, run_id, key, job_id)

        @router.get(base + "/{key}/relevance-exports/{job_id}/file")
        def relevance_file(project_id: UUID, run_id: UUID, key: UUID, job_id: UUID) -> Response:
            return Response(
                relevance.download(project_id, run_id, key, job_id),
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                headers={"Content-Disposition": f'attachment; filename="keyword-library-relevance-{job_id}.xlsx"'},
            )

    @router.get(base)
    def history(project_id: UUID, run_id: UUID) -> list[LibraryReport]:
        return service.history(project_id, run_id)

    @router.post(base)
    def create(project_id: UUID, run_id: UUID, data: LibraryRequest) -> LibraryReport:
        return service.create(project_id, run_id, data)

    @router.get(base + "/{key}")
    def get(project_id: UUID, run_id: UUID, key: UUID) -> LibraryReport:
        return service.get(project_id, run_id, key)

    @router.put(base + "/{key}/entry")
    def edit(project_id: UUID, run_id: UUID, key: UUID, data: EntryEdit) -> LibraryReport:
        return service.edit(project_id, run_id, key, data)

    @router.post(base + "/{key}/publish")
    def publish(project_id: UUID, run_id: UUID, key: UUID, data: PublishRequest) -> LibraryReport:
        return service.publish(project_id, run_id, key, data)

    @router.post(base + "/{key}/attachments/refresh")
    def refresh_attachments(project_id: UUID, run_id: UUID, key: UUID, data: RefreshAttachmentsRequest) -> LibraryReport:
        return service.refresh_attachments(project_id, run_id, key, data.expected_version)

    @router.get(base + "/{key}/export")
    def export(project_id: UUID, run_id: UUID, key: UUID, ranked_only: bool = False) -> Response:
        return Response(
            service.export(project_id, run_id, key, ranked_only=ranked_only).encode("utf-8-sig"),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="library-{key}.csv"'},
        )

    @router.get(base + "/{key}/template")
    def template(
        project_id: UUID, run_id: UUID, key: UUID, ranked_only: bool = False
    ) -> TemplateData:
        report = service.get(project_id, run_id, key)
        return template_data(report, service.template_roots(report), ranked_only=ranked_only)

    @router.get(base + "/{key}/template.xlsx")
    def xlsx(project_id: UUID, run_id: UUID, key: UUID, ranked_only: bool = False) -> Response:
        report = service.get(project_id, run_id, key)
        return Response(
            export_xlsx(report, service.template_roots(report), ranked_only=ranked_only),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="keyword-library-{key}.xlsx"'},
        )

    return router
