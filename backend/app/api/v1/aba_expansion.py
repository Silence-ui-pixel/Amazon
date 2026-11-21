"""第四步显式采集、只读恢复、分页和 CSV 下载。"""

from datetime import date
from typing import Annotated
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Query
from fastapi.responses import Response

from app.domain.aba_expansion import (
    ExpansionPage,
    ExpansionReport,
    ExpansionRequest,
    ExpansionWorkspace,
)
from app.services.aba_expansion import ExpansionService


def build_expansion_router(service: ExpansionService) -> APIRouter:
    router = APIRouter(tags=["aba-expansion"])
    base = "/api/v1/projects/{project_id}/pipeline-runs/{run_id}/aba-expansion"

    @router.get(base)
    def workspace(
        project_id: UUID, run_id: UUID, week_end: date | None = None
    ) -> ExpansionWorkspace:
        return service.workspace(project_id, run_id, week_end)

    @router.post(base)
    def collect(project_id: UUID, run_id: UUID, data: ExpansionRequest) -> ExpansionReport:
        return service.collect(project_id, run_id, data)

    @router.get(base + "/{report_id}")
    def get(project_id: UUID, run_id: UUID, report_id: UUID) -> ExpansionReport:
        return service.get(project_id, run_id, report_id)

    @router.get(base + "/{report_id}/rows")
    def rows(
        project_id: UUID,
        run_id: UUID,
        report_id: UUID,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=200)] = 20,
    ) -> ExpansionPage:
        return service.page(project_id, run_id, report_id, offset, limit)

    @router.get(base + "/{report_id}/export")
    def export(project_id: UUID, run_id: UUID, report_id: UUID) -> Response:
        report = service.get(project_id, run_id, report_id)
        return Response(
            service.export(project_id, run_id, report_id),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="aba-{report.direction}-{report.week_end}.csv"'
            },
        )

    @router.get(base + "/{report_id}/queries/{query_index}/export.xlsx")
    def query_xlsx(
        project_id: UUID, run_id: UUID, report_id: UUID, query_index: int
    ) -> Response:
        filename, content = service.query_workbook(project_id, run_id, report_id, query_index)
        return Response(
            content,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": "attachment; filename*=UTF-8''" + quote(filename)
            },
        )

    return router
