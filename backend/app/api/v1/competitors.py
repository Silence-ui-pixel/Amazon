"""竞对候选与人工选择；读取不触发外部采集。"""

from uuid import UUID

from fastapi import APIRouter
from fastapi.responses import FileResponse

from app.domain.competitors import (
    Competitors,
    DiscoveryInput,
    PoeBatchDownloadInput,
    PoeDownloadInput,
    SelectionInput,
)
from app.services.competitors import CompetitorService


def build_competitor_router(service: CompetitorService) -> APIRouter:
    router = APIRouter(tags=["competitors"])
    base = "/api/v1/projects/{project_id}/pipeline-runs/{run_id}/competitors"

    @router.get(base)
    def get(project_id: UUID, run_id: UUID) -> Competitors:
        return service.get(project_id, run_id)

    @router.post(base + "/discover")
    def discover(project_id: UUID, run_id: UUID, data: DiscoveryInput) -> Competitors:
        return service.discover(project_id, run_id, data)

    @router.post(base + "/poe-download")
    def download_poe(project_id: UUID, run_id: UUID, data: PoeDownloadInput) -> Competitors:
        return service.download_poe(project_id, run_id, data)

    @router.post(base + "/poe-download-all")
    def download_poe_all(
        project_id: UUID, run_id: UUID, data: PoeBatchDownloadInput
    ) -> Competitors:
        return service.download_poe_all(project_id, run_id, data.expected_version, data.retry_keywords)

    @router.get(base + "/poe-downloads/{artifact_id}")
    def original(project_id: UUID, run_id: UUID, artifact_id: UUID) -> FileResponse:
        path = service.poe_file(project_id, run_id, artifact_id)
        return FileResponse(path, media_type="text/csv", filename=f"poe-products-{artifact_id}.csv")

    @router.put(base + "/selection")
    def select(project_id: UUID, run_id: UUID, data: SelectionInput) -> Competitors:
        return service.select(project_id, run_id, data)

    return router
