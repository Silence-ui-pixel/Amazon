"""研究范围内的平台原件、计算表和下载重试。"""
from uuid import UUID
from urllib.parse import quote
from fastapi import APIRouter, Response
from app.services.source_exports import export_name


def build_source_exports_router(service):
    router = APIRouter(prefix='/api/v1/projects/{project_id}/pipeline-runs/{run_id}/source-exports', tags=['source-exports'])

    @router.get('')
    def list_exports(project_id: UUID, run_id: UUID):
        service.repository.get_run(project_id, run_id)
        return [service.public(item) for item in service.store.list(project_id, run_id)]

    @router.post('/{export_id}/retry')
    def retry(project_id: UUID, run_id: UUID, export_id: str):
        return service.public(service.retry(project_id, run_id, export_id))

    @router.get('/{export_id}/file')
    def download(project_id: UUID, run_id: UUID, export_id: str, original: bool = False):
        service.repository.get_run(project_id, run_id)
        item = service.store.get(project_id, run_id, export_id)
        raw = service.store.file(item, original)
        name = item['filename'] if original else export_name(item)
        return Response(raw, media_type='application/octet-stream', headers={'Content-Disposition': "attachment; filename*=UTF-8''" + quote(name)})

    return router
