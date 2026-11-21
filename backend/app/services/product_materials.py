"""从持久化输入整理商品资料，不下载远端图片，也不调用模型或商品数据源。"""

from uuid import UUID

from app.db.repository import Repository
from app.domain.product_materials import (
    MaterialImage,
    ProductMaterials,
    RemoteMaterial,
    UploadedMaterial,
)
from app.services.asin_product import AsinProductService
from app.services.images import ImageService


class ProductMaterialsService:
    """用稳定任务 ID 查询资料；保留原输入和来源，便于后续编排复用。"""

    def __init__(
        self,
        repository: Repository,
        images: ImageService,
        asin_products: AsinProductService | None = None,
    ) -> None:
        self.repository = repository
        self.images = images
        self.asin_products = asin_products or AsinProductService(repository)

    def get(self, project_id: UUID, run_id: UUID) -> ProductMaterials:
        run = self.repository.get_run(project_id, run_id)
        data = run.input
        images: list[MaterialImage] = []
        for image_id in data.image_ids:
            # 不能仅凭数据库记录认定文件可用，查询时仍检查项目归属和文件存在。
            asset, _path = self.images.content(project_id, image_id)
            images.append(UploadedMaterial(url=asset.content_url, asset=asset))
        images.extend(RemoteMaterial(url=str(url)) for url in data.image_urls)
        warnings: list[str] = []
        resolved = self.asin_products.get(project_id, run_id) if data.asin else None
        if data.asin and not resolved:
            warnings.append("ASIN 详情尚未读取，点击识别商品时将自动读取商品页。")
        if data.image_urls:
            warnings.append("远端图片仅保存地址，尚未下载或验证内容。")
        if not images:
            if data.asin:
                warnings.append("搜索核验将以 ASIN 精确匹配；无需额外上传主图。")
            else:
                warnings.append("尚无商品图片；后续主图匹配需要补充并确认自有商品主图。")
        else:
            warnings.append("图片用途尚未确认，不能自动认定为自有商品主图。")
        # 资料查询同步识别状态，避免识别完成后仍报告尚未开始。
        with self.repository.connect() as conn:
            row = conn.execute(
                "SELECT status FROM product_understandings WHERE run_id = ?", (str(run_id),)
            ).fetchone()
        return ProductMaterials(
            model_status=row[0] if row else "not_started",
            project_id=project_id,
            run_id=run_id,
            marketplace=data.marketplace,
            asin=data.asin,
            supplied_product_name=data.product_name,
            supplied_description=data.description,
            reference_price=data.reference_price,
            reference_currency=data.reference_currency,
            images=images,
            asin_resolution="resolved"
            if resolved
            else ("awaiting_provider" if data.asin else "not_requested"),
            asin_product=resolved,
            warnings=warnings,
        )
