"""ABA 接口：显式采集，读取历史不会触发网络请求。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from app.domain.aba import AbaConnectionStatus, AbaReport, AbaRequest
from app.domain.core_terms import HotSearchCoreItem
from app.services.aba import AbaService
from app.services.core_terms import CoreService


def build_aba_router(service: AbaService) -> APIRouter:
    router = APIRouter(tags=["aba"])
    base = "/api/v1/projects/{project_id}/pipeline-runs/{run_id}/aba-reports"

    @router.get("/api/v1/aba/status")
    def status() -> AbaConnectionStatus:
        return service.client.status()

    @router.post(base)
    def collect(project_id: UUID, run_id: UUID, data: AbaRequest) -> AbaReport:
        return service.collect(project_id, run_id, data)

    @router.get(base)
    def history(
        project_id: UUID,
        run_id: UUID,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=20)] = 5,
    ) -> list[AbaReport]:
        return service.history(project_id, run_id, offset, limit)

    @router.get(base + "/{report_id}")
    def get(project_id: UUID, run_id: UUID, report_id: UUID) -> AbaReport:
        return service.get(project_id, run_id, report_id)

    @router.get(base + "/{report_id}/hot-search-candidates")
    def hot_search_candidates(
        project_id: UUID, run_id: UUID, report_id: UUID
    ) -> list[HotSearchCoreItem]:
        return CoreService(service.repository, service, None).hot_search_candidates(
            project_id, run_id, report_id
        )

    return router
