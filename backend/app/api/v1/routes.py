"""第一步输入接口：同步保存输入，不启动 LangGraph 或异步采集。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Response
from pydantic import StringConstraints

from app.db.repository import Repository
from app.domain.errors import DomainError
from app.domain.product_materials import ProductMaterials
from app.domain.schemas import PipelineRun, ProductInput, Project, ProjectCreate
from app.services.product_materials import ProductMaterialsService

IdempotencyKey = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)
]


def build_router(repository: Repository, materials: ProductMaterialsService) -> APIRouter:
    """注入数据仓库，让运行服务与测试使用各自独立的数据库。"""
    router = APIRouter(prefix="/api/v1")

    @router.get("/health/live", tags=["health"])
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/health/ready", tags=["health"])
    def ready() -> dict[str, str]:
        if not repository.ready():
            raise DomainError(503, "NOT_READY", "数据库未就绪")
        return {"status": "ready"}

    @router.post("/projects", status_code=201, tags=["projects"])
    def create_project(data: ProjectCreate, response: Response) -> Project:
        result = repository.create_project(data)
        response.headers["Location"] = f"/api/v1/projects/{result.id}"
        return result

    @router.get("/projects/{project_id}", tags=["projects"])
    def get_project(project_id: UUID) -> Project:
        return repository.get_project(project_id)

    @router.post("/projects/{project_id}/pipeline-runs", status_code=201, tags=["input"])
    def create_run(
        project_id: UUID,
        data: ProductInput,
        response: Response,
        idempotency_key: Annotated[IdempotencyKey, Header(alias="Idempotency-Key")],
    ) -> PipelineRun:
        # 当前仅创建输入记录，因此返回 201；尚未入队执行，不能返回 202。
        result = repository.create_run(project_id, data, idempotency_key)
        response.headers["Location"] = f"/api/v1/projects/{project_id}/pipeline-runs/{result.id}"
        # ETag 标识保存的记录版本；更新和人工审核恢复接口留待后续实现。
        response.headers["ETag"] = f'"{result.version}"'
        return result

    @router.get("/projects/{project_id}/pipeline-runs/{run_id}", tags=["input"])
    def get_run(project_id: UUID, run_id: UUID, response: Response) -> PipelineRun:
        result = repository.get_run(project_id, run_id)
        # ETag 标识保存的记录版本；更新和人工审核恢复接口留待后续实现。
        response.headers["ETag"] = f'"{result.version}"'
        return result

    @router.get("/projects/{project_id}/pipeline-runs/{run_id}/product-materials", tags=["product"])
    def get_product_materials(project_id: UUID, run_id: UUID) -> ProductMaterials:
        # 只读视图没有触发外部工作，返回 200，任务状态保持 input_received。
        return materials.get(project_id, run_id)

    return router
