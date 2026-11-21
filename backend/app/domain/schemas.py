"""商品输入与响应契约：集中校验业务参数，不访问网络或数据库。"""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    HttpUrl,
    StringConstraints,
    model_validator,
)

ImageMime = Literal["image/jpeg", "image/png", "image/webp"]
ImageExtension = Literal[".jpg", ".png", ".webp"]


def _us_only(value: object) -> object:
    """当前只做美国站；统一大小写并给出明确提示，不保留其他站点暗示。"""
    if isinstance(value, str):
        normalized = value.strip().upper()
        if normalized != "US":
            raise ValueError("当前仅支持美国站（US）")
        return normalized
    return value


Marketplace = Annotated[Literal["US"], BeforeValidator(_us_only)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Asin = Annotated[
    str, StringConstraints(strip_whitespace=True, to_upper=True, pattern=r"^[A-Za-z0-9]{10}$")
]


class Contract(BaseModel):
    """拒绝未知字段，尽早发现前端字段拼写或接口版本不匹配。"""

    model_config = ConfigDict(extra="forbid")


class ProjectCreate(Contract):
    name: Name
    marketplace: Marketplace


class Project(ProjectCreate):
    id: UUID
    created_at: datetime


class ProductInput(Contract):
    """站点必填；ASIN 或图片至少一项，名称与描述仅作补充。"""

    marketplace: Marketplace
    product_name: Name | None = None
    asin: Asin | None = None
    # 当前只校验并保存图片地址，不下载图片，也不据此声称已识别商品。
    image_urls: list[HttpUrl] = Field(default_factory=list[HttpUrl], max_length=5)
    # 本地上传图片使用稳定 ID 引用，避免把文件数据放进后续编排状态。
    image_ids: list[UUID] = Field(default_factory=list[UUID], max_length=5)
    description: str = Field(default="", max_length=10000)
    # 用户填写的本品售价，不从图片推断；可空以兼容旧研究记录。
    reference_price: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=4)
    reference_currency: Literal["USD"] = "USD"

    @model_validator(mode="after")
    def require_product_input(self) -> "ProductInput":
        """字段校验后要求 ASIN 或图片，名称和描述不能替代商品识别依据。"""
        if len(self.image_urls) + len(self.image_ids) > 5:
            raise ValueError("图片地址与上传图片合计最多 5 张")
        if len(set(self.image_ids)) != len(self.image_ids):
            raise ValueError("不能重复引用同一张图片")
        if not (self.asin or self.image_urls or self.image_ids):
            raise ValueError("ASIN 或图片至少提供一项，可同时提供")
        return self


class ImageAsset(Contract):
    """已验证图片的持久化元数据，文件地址为项目内受控接口。"""

    id: UUID
    project_id: UUID
    original_name: str
    content_type: ImageMime
    extension: ImageExtension
    size_bytes: int
    width: int
    height: int
    sha256: str
    created_at: datetime
    content_url: str


class PipelineRun(Contract):
    """保存待编排的输入；input_received 只表示已接收，不表示模型已执行。"""

    id: UUID
    project_id: UUID
    status: Literal["input_received"] = "input_received"
    version: Literal[1] = 1
    input: ProductInput
    created_at: datetime
