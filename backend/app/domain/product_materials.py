"""供商品理解阶段使用的统一资料契约，明确来源与尚未完成的外部工作。"""

from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from app.domain.asin_product import ProductSnapshot
from app.domain.schemas import Contract, ImageAsset, Marketplace


class UploadedMaterial(Contract):
    """元数据在上传时通过内容校验；role 不自动推断为商品主图。"""

    source: Literal["uploaded"] = "uploaded"
    verification: Literal["verified_at_upload"] = "verified_at_upload"
    role: Literal["unspecified"] = "unspecified"
    url: str
    asset: ImageAsset


class RemoteMaterial(Contract):
    """只有地址，尚未下载或验证，因此不能声明为可用图片证据。"""

    source: Literal["remote_url"] = "remote_url"
    verification: Literal["unverified"] = "unverified"
    role: Literal["unspecified"] = "unspecified"
    url: str
    asset: None = None


MaterialImage = Annotated[UploadedMaterial | RemoteMaterial, Field(discriminator="source")]


class ProductMaterials(Contract):
    """已保存输入的只读资料视图，不表示商品识别或 ASIN 补全已完成。"""

    schema_version: Literal[1] = 1
    project_id: UUID
    run_id: UUID
    marketplace: Marketplace
    asin: str | None
    # 用户提供的文字与未来模型/供应商推断值分开，避免来源混淆。
    supplied_product_name: str | None
    supplied_description: str
    reference_price: Decimal | None = None
    reference_currency: Literal["USD"] = "USD"
    images: list[MaterialImage]
    asin_resolution: Literal["awaiting_provider", "not_requested", "resolved"]
    asin_product: ProductSnapshot | None = None
    model_status: Literal["not_started", "running", "succeeded", "failed", "uncertain"] = (
        "not_started"
    )
    warnings: list[str]
