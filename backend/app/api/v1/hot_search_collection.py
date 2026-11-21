"""第二步 v2 采集作业、单商品词重试与工作簿下载。"""

from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Response
from pydantic import BaseModel

from app.domain.hot_search_collection import HotSearchCollectionJob
from app.services.hot_search_collection import HotSearchCollectionService


class RetryTerm(BaseModel):
    product_term: str


def build_hot_search_collection_router(service: HotSearchCollectionService) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/projects/{project_id}/pipeline-runs/{run_id}",
        tags=["hot-search-collection"],
    )

    @router.post("/hot-search-collections")
    def start(
        project_id: UUID, run_id: UUID, background: BackgroundTasks
    ) -> HotSearchCollectionJob:
        job = service.start(project_id, run_id)
        if job.status == "queued":
            background.add_task(service.execute, project_id, run_id, job.id)
        return job

    @router.get("/hot-search-collections/latest")
    def latest(project_id: UUID, run_id: UUID) -> HotSearchCollectionJob:
        return service.latest(project_id, run_id)

    @router.get("/hot-search-collections/{job_id}")
    def get(project_id: UUID, run_id: UUID, job_id: UUID) -> HotSearchCollectionJob:
        return service.get(project_id, run_id, job_id)

    @router.post("/hot-search-collections/{job_id}/retry")
    def retry(
        project_id: UUID, run_id: UUID, job_id: UUID, data: RetryTerm
    ) -> HotSearchCollectionJob:
        return service.retry(project_id, run_id, job_id, data.product_term)

    @router.get("/hot-search-collections/{job_id}/workbook.xlsx")
    def workbook(project_id: UUID, run_id: UUID, job_id: UUID) -> Response:
        return Response(
            service.workbook(project_id, run_id, job_id),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": 'attachment; filename="hot-search-workbook.xlsx"'},
        )

    return router
