"""编排端点：启动、查询、恢复、终止；状态由图 checkpoint 与投影表承载。"""

from uuid import UUID

from fastapi import APIRouter

from app.domain.workflow import ResumeRequest, StartWorkflowRequest, WorkflowView
from app.workflows.service import WorkflowService


def build_workflow_router(service: WorkflowService) -> APIRouter:
    router = APIRouter(tags=["workflow"])
    base = "/api/v1/projects/{project_id}/pipeline-runs/{run_id}/workflow"

    @router.post(base, status_code=202)
    def start(project_id: UUID, run_id: UUID, data: StartWorkflowRequest) -> WorkflowView:
        return service.start(project_id, run_id, data.options, data.browser_device_id)

    @router.get(base + "/history")
    def history(project_id: UUID, run_id: UUID) -> list[WorkflowView]:
        return service.history(project_id, run_id)

    @router.get(base)
    def latest(project_id: UUID, run_id: UUID) -> WorkflowView:
        return service.view(project_id, run_id)

    @router.get(base + "/{workflow_id}")
    def get(project_id: UUID, run_id: UUID, workflow_id: str) -> WorkflowView:
        return service.view(project_id, run_id, workflow_id)

    @router.post(base + "/{workflow_id}/resume", status_code=202)
    def resume(
        project_id: UUID, run_id: UUID, workflow_id: str, data: ResumeRequest
    ) -> WorkflowView:
        return service.resume(project_id, run_id, workflow_id, data)

    @router.post(base + "/{workflow_id}/abort", status_code=202)
    def abort(project_id: UUID, run_id: UUID, workflow_id: str) -> WorkflowView:
        return service.abort(project_id, run_id, workflow_id)

    @router.post(base + "/{workflow_id}/retry-traffic", status_code=202)
    def retry_traffic(
        project_id: UUID, run_id: UUID, workflow_id: str, data: ResumeRequest
    ) -> WorkflowView:
        return service.retry_traffic(project_id, run_id, workflow_id, data)

    @router.post(base + "/{workflow_id}/retry-cleaning", status_code=202)
    def retry_cleaning(
        project_id: UUID, run_id: UUID, workflow_id: str, data: ResumeRequest
    ) -> WorkflowView:
        return service.retry_cleaning(project_id, run_id, workflow_id, data)

    return router
