"""第五步文件入口，上传读取限额，解析在工作线程执行。"""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, File, Form, Query, UploadFile
from fastapi.responses import Response
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from app.adapters.third_party import MAX_BYTES
from app.domain.errors import DomainError
from app.domain.sellersprite import BrowserCollectionInput, BrowserCollectionJob
from app.domain.third_party import (
    ImportConfirmation,
    ImportMetadata,
    ImportPage,
    ImportReport,
    ImportWorkspace,
)
from app.services.sellersprite_collection import SellerspriteCollectionService
from app.services.third_party import ThirdPartyService


def build_third_party_router(
    service: ThirdPartyService, collector: SellerspriteCollectionService
) -> APIRouter:
    router = APIRouter(tags=["third-party"])
    base = "/api/v1/projects/{project_id}/pipeline-runs/{run_id}/third-party"

    @router.get(base)
    def workspace(
        project_id: UUID,
        run_id: UUID,
        period_kind: Literal["date_range", "last_30_days"] | None = None,
    ) -> ImportWorkspace:
        return service.workspace(project_id, run_id, period_kind)

    @router.get(base + "/browser-jobs")
    def jobs(project_id: UUID, run_id: UUID) -> list[BrowserCollectionJob]:
        return collector.list(project_id, run_id)

    @router.post(base + "/browser-jobs", status_code=202)
    def collect(
        project_id: UUID, run_id: UUID, data: BrowserCollectionInput
    ) -> BrowserCollectionJob:
        return collector.create(project_id, run_id, data)

    @router.get(base + "/{report_id}/original")
    def original(project_id: UUID, run_id: UUID, report_id: UUID) -> Response:
        report = service.get(project_id, run_id, report_id)
        from urllib.parse import quote

        return Response(
            service.original(project_id, run_id, report_id),
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": "attachment; filename*=UTF-8''" + quote(report.filename)
            },
        )

    @router.post(base + "/preview")
    async def preview(
        project_id: UUID,
        run_id: UUID,
        file: Annotated[UploadFile, File()],
        metadata: Annotated[str, Form(max_length=5000)],
    ) -> ImportReport:
        try:
            try:
                parsed = ImportMetadata.model_validate_json(metadata)
            except ValidationError:
                raise DomainError(
                    422, "IMPORT_METADATA_INVALID", "请核对查询条件、站点、日期和总行数"
                ) from None
            content = await file.read(MAX_BYTES + 1)
            return await run_in_threadpool(
                service.preview, project_id, run_id, file.filename or "词表", content, parsed
            )
        finally:
            await file.close()

    @router.post(base + "/{report_id}/confirm")
    def confirm(
        project_id: UUID, run_id: UUID, report_id: UUID, data: ImportConfirmation
    ) -> ImportReport:
        return service.confirm(project_id, run_id, report_id, data)

    @router.get(base + "/{report_id}/rows")
    def rows(
        project_id: UUID,
        run_id: UUID,
        report_id: UUID,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=200)] = 20,
    ) -> ImportPage:
        return service.page(project_id, run_id, report_id, offset, limit)

    @router.get(base + "/{report_id}/export")
    def export(project_id: UUID, run_id: UUID, report_id: UUID) -> Response:
        service.get(project_id, run_id, report_id)
        return Response(
            service.export(project_id, run_id, report_id),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="sellersprite-{report_id}.csv"'},
        )

    return router
