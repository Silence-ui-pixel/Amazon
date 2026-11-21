"""第七步流量筛选 API。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query
from fastapi.responses import Response

from app.domain.traffic import (
    BatchTrafficReview,
    ReviewRecord,
    TrafficPage,
    TrafficReport,
    TrafficRequest,
    TrafficReview,
    TrafficReviewProgress,
)
from app.services.traffic import TrafficService
from app.services.traffic_auto_review import TrafficAutoReviewService


def build_traffic_router(service: TrafficService, automatic: TrafficAutoReviewService) -> APIRouter:
    router = APIRouter(tags=["traffic"])
    base = "/api/v1/projects/{project_id}/pipeline-runs/{run_id}/traffic"

    @router.get(base)
    def history(project_id: UUID, run_id: UUID) -> list[TrafficReport]:
        return service.history(project_id, run_id)

    @router.post(base)
    def create(
        project_id: UUID, run_id: UUID, data: TrafficRequest, auto_review: bool = False
    ) -> TrafficReport:
        report = service.create(project_id, run_id, data)
        if auto_review:
            automatic.start(project_id, run_id, report.id)
        return report

    @router.get(base + "/{analysis_id}/auto-review")
    def progress(project_id: UUID, run_id: UUID, analysis_id: UUID) -> TrafficReviewProgress:
        return automatic.get(project_id, run_id, analysis_id)

    @router.post(base + "/{analysis_id}/auto-review", status_code=202)
    def start_review(project_id: UUID, run_id: UUID, analysis_id: UUID) -> TrafficReviewProgress:
        return automatic.start(project_id, run_id, analysis_id)

    @router.get(base + "/{analysis_id}")
    def get(project_id: UUID, run_id: UUID, analysis_id: UUID) -> TrafficReport:
        return service.get(project_id, run_id, analysis_id)

    @router.get(base + "/{analysis_id}/rows")
    def page(
        project_id: UUID,
        run_id: UUID,
        analysis_id: UUID,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=200)] = 20,
    ) -> TrafficPage:
        return service.page(project_id, run_id, analysis_id, offset, limit)

    @router.get(base + "/{analysis_id}/reviews")
    def reviews(project_id: UUID, run_id: UUID, analysis_id: UUID) -> list[ReviewRecord]:
        return service.reviews(project_id, run_id, analysis_id)

    @router.put(base + "/{analysis_id}/review")
    def review(
        project_id: UUID, run_id: UUID, analysis_id: UUID, data: TrafficReview
    ) -> TrafficReport:
        return service.review(project_id, run_id, analysis_id, data)

    @router.get(base + "/{analysis_id}/export")
    def export(project_id: UUID, run_id: UUID, analysis_id: UUID) -> Response:
        return Response(
            service.export(project_id, run_id, analysis_id).encode("utf-8-sig"),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="traffic-{analysis_id}.csv"'},
        )

    @router.put(base + "/{analysis_id}/batch-review")
    def batch_review(
        project_id: UUID, run_id: UUID, analysis_id: UUID, data: BatchTrafficReview
    ) -> TrafficReport:
        return service.batch_review(project_id, run_id, analysis_id, data)

    return router
