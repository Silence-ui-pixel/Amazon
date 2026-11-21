"""第六步接口：分析、分页证据及人工审核。"""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query
from fastapi.responses import Response

from app.domain.cleaning import CleaningPage, CleaningReport, CleaningRequest, Review
from app.services.cleaning import CleaningService


def build_cleaning_router(service: CleaningService) -> APIRouter:
    router = APIRouter(tags=["cleaning"])
    base = "/api/v1/projects/{project_id}/pipeline-runs/{run_id}/cleaning"

    @router.get(base)
    def history(project_id: UUID, run_id: UUID) -> list[CleaningReport]:
        return service.history(project_id, run_id)

    @router.post(base)
    def create(project_id: UUID, run_id: UUID, data: CleaningRequest) -> CleaningReport:
        return service.create(project_id, run_id, data)

    @router.get(base + "/{analysis_id}")
    def get(project_id: UUID, run_id: UUID, analysis_id: UUID) -> CleaningReport:
        return service.get(project_id, run_id, analysis_id)

    @router.get(base + "/{analysis_id}/classification-progress")
    def classification_progress(project_id: UUID, run_id: UUID, analysis_id: UUID) -> dict[str, object]:
        return service.classification_progress(project_id, run_id, analysis_id)

    @router.get(base + "/{analysis_id}/rows")
    def rows(
        project_id: UUID,
        run_id: UUID,
        analysis_id: UUID,
        kind: Literal["phrases", "frequencies"] = "frequencies",
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=200)] = 20,
    ) -> CleaningPage:
        return service.page(project_id, run_id, analysis_id, kind, offset, limit)

    @router.put(base + "/{analysis_id}/review")
    def review(project_id: UUID, run_id: UUID, analysis_id: UUID, data: Review) -> CleaningReport:
        return service.review(project_id, run_id, analysis_id, data)

    @router.post(base + "/{analysis_id}/classify")
    def classify(
        project_id: UUID,
        run_id: UUID,
        analysis_id: UUID,
        retry_failed: bool = False,
    ) -> CleaningReport:
        return service.classify(project_id, run_id, analysis_id, retry_failed=retry_failed)

    @router.get(base + "/{analysis_id}/export")
    def export(project_id: UUID, run_id: UUID, analysis_id: UUID) -> Response:
        return Response(
            service.export(project_id, run_id, analysis_id).encode("utf-8-sig"),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="cleaning-{analysis_id}.csv"'},
        )

    return router
