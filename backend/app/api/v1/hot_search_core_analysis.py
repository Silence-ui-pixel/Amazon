"""POST creates a durable queued analysis; refresh only reads persisted evidence."""

from uuid import UUID

from fastapi import APIRouter, BackgroundTasks

from app.services.hot_search_core_analysis import (
    HotSearchCoreAnalysis,
    HotSearchCoreAnalysisRequest,
    HotSearchCoreAnalysisService,
)


def build_hot_search_core_analysis_router(service: HotSearchCoreAnalysisService) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/projects/{project_id}/pipeline-runs/{run_id}",
        tags=["hot-search-core-analysis"],
    )

    @router.post("/hot-search-core-analyses")
    def start(
        project_id: UUID, run_id: UUID, data: HotSearchCoreAnalysisRequest
    ) -> HotSearchCoreAnalysis:
        return service.start(
            project_id,
            run_id,
            data.report_id,
            data.own_brands,
            retry_incomplete=data.retry_incomplete,
            retry_unknown_model_outcomes=data.retry_unknown_model_outcomes,
            force_reanalysis=data.force_reanalysis,
        )

    @router.get("/hot-search-core-analysis")
    def latest(project_id: UUID, run_id: UUID) -> HotSearchCoreAnalysis:
        return service.get(project_id, run_id)

    @router.get("/hot-search-core-analyses/{analysis_id}")
    def get(project_id: UUID, run_id: UUID, analysis_id: UUID) -> HotSearchCoreAnalysis:
        return service.get(project_id, run_id, analysis_id)

    @router.post("/hot-search-core-analyses/{analysis_id}/execute", status_code=202)
    def execute(
        project_id: UUID, run_id: UUID, analysis_id: UUID, background: BackgroundTasks
    ) -> HotSearchCoreAnalysis:
        analysis = service.get(project_id, run_id, analysis_id)
        if analysis.status == "queued":
            # The persisted ID exists before dispatch; execute atomically claims queued once.
            background.add_task(service.execute, project_id, run_id, analysis_id)
        return analysis

    return router
