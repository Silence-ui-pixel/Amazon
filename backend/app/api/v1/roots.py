"""真实词根推荐和人工确认端点，不启动后续采集。"""

from uuid import UUID

from fastapi import APIRouter, BackgroundTasks

from app.domain.roots import (
    RootCandidates,
    RootCandidatesEdit,
    RootCandidatesRegenerate,
    RootCandidateVersion,
    RootSelection,
    RootSelectionInput,
)
from app.services.product_term_analysis import ProductTermAnalysisService
from app.services.roots import RootService


def build_root_router(
    service: RootService, analysis: ProductTermAnalysisService | None = None
) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/projects/{project_id}/pipeline-runs/{run_id}", tags=["roots"]
    )

    @router.post("/root-candidates")
    def generate(project_id: UUID, run_id: UUID) -> RootCandidates:
        return service.generate(project_id, run_id)

    @router.get("/root-candidates")
    def get(project_id: UUID, run_id: UUID) -> RootCandidates:
        return service.get(project_id, run_id)

    @router.put("/product-term-candidates")
    def revise(project_id: UUID, run_id: UUID, data: RootCandidatesEdit) -> RootCandidates:
        return service.revise(project_id, run_id, data)

    @router.post("/product-term-candidates/regenerate")
    def regenerate(project_id: UUID, run_id: UUID, data: RootCandidatesRegenerate) -> RootCandidates:
        return service.generate(project_id, run_id, expected_version=data.expected_version)

    @router.get("/product-term-candidates/history")
    def history(project_id: UUID, run_id: UUID) -> list[RootCandidateVersion]:
        current = service.get(project_id, run_id)
        return [*current.history, RootCandidateVersion.model_validate(current.model_dump(exclude={"history"}))]

    @router.put("/root-selection")
    def select(project_id: UUID, run_id: UUID, data: RootSelectionInput) -> RootSelection:
        return service.select(project_id, run_id, data)

    @router.get("/root-selection")
    def selection(project_id: UUID, run_id: UUID) -> RootSelection:
        return service.get_selection(project_id, run_id)

    @router.post("/product-term-candidates")
    def generate_product_terms(project_id: UUID, run_id: UUID, tasks: BackgroundTasks) -> RootCandidates:
        result = service.generate(project_id, run_id)
        if analysis is not None:
            job = analysis.enqueue(project_id, run_id)
            if job.status == "queued":
                tasks.add_task(analysis.execute_queued, project_id, run_id, job.id)
        return result

    @router.get("/product-term-candidates")
    def get_product_terms(project_id: UUID, run_id: UUID) -> RootCandidates:
        return service.get(project_id, run_id)

    @router.put("/product-term-selection")
    def select_product_terms(
        project_id: UUID, run_id: UUID, data: RootSelectionInput
    ) -> RootSelection:
        return service.select(project_id, run_id, data)

    @router.get("/product-term-selection")
    def product_term_selection(project_id: UUID, run_id: UUID) -> RootSelection:
        return service.get_selection(project_id, run_id)

    return router
