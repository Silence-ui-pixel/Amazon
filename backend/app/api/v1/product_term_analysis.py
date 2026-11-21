"""商品词相关度分析端点；POST 自动执行全部候选，GET 只读取。"""

from uuid import UUID

from fastapi import APIRouter, BackgroundTasks

from app.domain.errors import DomainError
from app.domain.product_terms import ProductTermAnalysis, TermAnalysisItem
from app.services.product_term_analysis import ProductTermAnalysisService


def build_product_term_analysis_router(service: ProductTermAnalysisService) -> APIRouter:
    def compact(result: ProductTermAnalysis) -> ProductTermAnalysis:
        return result.model_copy(update={"items": [item.model_copy(update={"collection": None, "comparisons": [], "evidence_loaded": False}) for item in result.items]})

    router = APIRouter(
        prefix="/api/v1/projects/{project_id}/pipeline-runs/{run_id}",
        tags=["product-term-analysis"],
    )

    @router.post("/product-term-analyses")
    def start(project_id: UUID, run_id: UUID, tasks: BackgroundTasks, refresh: bool = False) -> ProductTermAnalysis:
        result = service.enqueue(project_id, run_id, refresh=refresh)
        if result.status == "queued":
            tasks.add_task(service.execute_queued, project_id, run_id, result.id)
        return result

    @router.get("/product-term-analyses/history")
    def history(project_id: UUID, run_id: UUID) -> list[ProductTermAnalysis]:
        return [compact(result) for result in service.history(project_id, run_id)]

    @router.get("/product-term-analyses/latest")
    def latest(project_id: UUID, run_id: UUID, summary: bool = False) -> ProductTermAnalysis:
        result = service.get(project_id, run_id)
        return compact(result) if summary else result

    @router.get("/product-term-analyses/{analysis_id}/candidates/{candidate_id}")
    def evidence(project_id: UUID, run_id: UUID, analysis_id: UUID, candidate_id: UUID) -> TermAnalysisItem:
        result = service.get(project_id, run_id, analysis_id)
        for item in result.items:
            if item.candidate_id == candidate_id:
                # 原始详情仍在数据库中；页面展示逐商品比较事实和搜索页证据即可。
                collection = item.collection.model_copy(update={"products": []}) if item.collection else None
                return item.model_copy(update={"collection": collection, "evidence_loaded": True})
        raise DomainError(404, "PRODUCT_TERM_EVIDENCE_NOT_FOUND", "该分析中不存在此商品词")

    @router.get("/product-term-analyses/{analysis_id}")
    def get(project_id: UUID, run_id: UUID, analysis_id: UUID) -> ProductTermAnalysis:
        return service.get(project_id, run_id, analysis_id)

    return router
