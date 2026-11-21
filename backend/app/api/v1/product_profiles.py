"""商品四维基准生成、读取与版本确认接口。"""

from uuid import UUID

from fastapi import APIRouter

from app.domain.product_terms import (
    ProductProfileGenerateRequest,
    ProductProfileRecord,
    ProductProfileRequest,
)
from app.services.product_profiles import ProductProfileService


def build_product_profile_router(service: ProductProfileService) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/projects/{project_id}/pipeline-runs/{run_id}", tags=["product-profile"]
    )

    @router.post("/product-profile")
    def generate(
        project_id: UUID,
        run_id: UUID,
        data: ProductProfileGenerateRequest | None = None,
    ) -> ProductProfileRecord:
        return service.generate(
            project_id,
            run_id,
            data.reference_price if data else None,
            data.currency if data else "USD",
        )

    @router.get("/product-profile")
    def get(project_id: UUID, run_id: UUID) -> ProductProfileRecord:
        return service.get(project_id, run_id)

    @router.get("/product-profile/history")
    def history(project_id: UUID, run_id: UUID) -> list[ProductProfileRecord]:
        return service.history(project_id, run_id)

    @router.put("/product-profile")
    def confirm(
        project_id: UUID, run_id: UUID, data: ProductProfileRequest
    ) -> ProductProfileRecord:
        return service.confirm(project_id, run_id, data)

    return router
