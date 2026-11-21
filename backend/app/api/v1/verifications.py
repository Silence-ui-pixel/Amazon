"""异步核验接口：创建返回202，轮询读取持久化进度。"""

from uuid import UUID

from fastapi import APIRouter
from fastapi.responses import FileResponse

from app.domain.verification import Verification, VerificationInput
from app.services.verification import VerificationService


def build_verification_router(service: VerificationService) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/projects/{project_id}/pipeline-runs/{run_id}", tags=["搜索核验"]
    )

    @router.post("/root-verifications", status_code=202)
    def start(project_id: UUID, run_id: UUID, data: VerificationInput) -> Verification:
        return service.start(project_id, run_id, data)

    @router.get("/root-verifications/{verification_id}")
    def get(project_id: UUID, run_id: UUID, verification_id: UUID) -> Verification:
        return service.get(project_id, run_id, verification_id)

    @router.post("/root-verifications/{verification_id}/resume", status_code=202)
    def resume(project_id: UUID, run_id: UUID, verification_id: UUID) -> Verification:
        return service.resume(project_id, run_id, verification_id)

    @router.post("/root-verifications/{verification_id}/review", status_code=202)
    def review(project_id: UUID, run_id: UUID, verification_id: UUID) -> Verification:
        return service.open_review(project_id, run_id, verification_id)

    @router.get("/root-verifications/{verification_id}/evidence/{filename}")
    def evidence(
        project_id: UUID, run_id: UUID, verification_id: UUID, filename: str
    ) -> FileResponse:
        path = service.evidence(project_id, run_id, verification_id, filename)
        return FileResponse(
            path, media_type="application/json" if path.suffix == ".json" else "image/png"
        )

    return router
