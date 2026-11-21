"""商品识别独立接口，显式 POST 才会产生模型调用。"""

from uuid import UUID

from fastapi import APIRouter

from app.domain.product_understanding import (
    ProductConfirmation,
    ProductConfirmationRequest,
    UnderstandingResult,
)
from app.services.product_confirmation import ProductConfirmationService
from app.services.product_understanding import ProductUnderstandingService


def build_understanding_router(service: ProductUnderstandingService) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/projects/{project_id}/pipeline-runs/{run_id}", tags=["product"]
    )
    confirmations = ProductConfirmationService(service.repository, service)

    @router.get("/product-confirmation")
    def get_confirmation(project_id: UUID, run_id: UUID) -> ProductConfirmation:
        return confirmations.get(project_id, run_id)

    @router.get("/product-confirmation/history")
    def confirmation_history(project_id: UUID, run_id: UUID) -> list[ProductConfirmation]:
        return confirmations.history(project_id, run_id)

    @router.put("/product-confirmation")
    def save_confirmation(
        project_id: UUID, run_id: UUID, data: ProductConfirmationRequest
    ) -> ProductConfirmation:
        return confirmations.save(project_id, run_id, data)

    @router.post("/product-understanding")
    def identify(project_id: UUID, run_id: UUID) -> UnderstandingResult:
        # 同步返回结果；任务唯一键在服务层阻止重复调用，不虚报异步队列。
        return service.identify(project_id, run_id)

    @router.get("/product-understanding")
    def get(project_id: UUID, run_id: UUID) -> UnderstandingResult:
        return service.get(project_id, run_id)

    return router
