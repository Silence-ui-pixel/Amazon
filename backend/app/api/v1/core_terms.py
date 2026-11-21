"""核心大词接口；显式分析、分页与人工确认。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from app.domain.core_terms import (
    CoreAnalysis,
    CoreCandidates,
    CoreRequest,
    CoreSelection,
    CoreSelectionInput,
)
from app.services.core_terms import CoreService


def build_core_router(service: CoreService) -> APIRouter:
    router = APIRouter(tags=["core-terms"])
    base = "/api/v1/projects/{project_id}/pipeline-runs/{run_id}"
    report = base + "/aba-reports/{report_id}"

    @router.get(report + "/core-candidates")
    def candidates(
        project_id: UUID,
        run_id: UUID,
        report_id: UUID,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=200)] = 20,
    ) -> CoreCandidates:
        return service.candidates(project_id, run_id, report_id, offset, limit)

    @router.post(report + "/core-analyses")
    def analyze(project_id: UUID, run_id: UUID, report_id: UUID, data: CoreRequest) -> CoreAnalysis:
        return service.analyze(project_id, run_id, report_id, data)

    @router.get(report + "/core-analyses")
    def history(project_id: UUID, run_id: UUID, report_id: UUID) -> list[CoreAnalysis]:
        return service.history(project_id, run_id, report_id)

    @router.get(base + "/core-analyses/{analysis_id}")
    def get(project_id: UUID, run_id: UUID, analysis_id: UUID) -> CoreAnalysis:
        return service.get(project_id, run_id, analysis_id)

    @router.get(base + "/core-selection")
    def selection(project_id: UUID, run_id: UUID) -> CoreSelection:
        return service.selection(project_id, run_id)

    @router.put(base + "/core-selection")
    def select(project_id: UUID, run_id: UUID, data: CoreSelectionInput) -> CoreSelection:
        """按显式规则版本确认指定分析；展示分页不限制保存集合。"""
        return service.select(project_id, run_id, data)

    return router
