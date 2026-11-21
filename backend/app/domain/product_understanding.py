"""商品识别结果契约：模型推断与用户输入分开保存。"""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StringConstraints

from app.domain.schemas import Contract, Name

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class ProductUnderstanding(Contract):
    product_name: Text
    product_type: Text
    features: list[Text] = Field(max_length=20)
    uncertainties: list[Text] = Field(max_length=20)


class UnderstandingResult(Contract):
    project_id: UUID
    run_id: UUID
    # 新记录标明实际供应商；bailian 仅用于兼容切换前已持久化的结果。
    provider: Literal["mimo", "bailian"] = "mimo"
    model: str
    created_at: datetime
    product: ProductUnderstanding
    warnings: list[str]


class ProductConfirmationRequest(Contract):
    expected_version: int = Field(ge=0)
    product: ProductUnderstanding
    reviewer: Name
    reason: Name


class ProductConfirmation(Contract):
    project_id: UUID
    run_id: UUID
    version: int
    created_at: datetime
    data: ProductConfirmationRequest
