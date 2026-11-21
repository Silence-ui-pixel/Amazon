"""Amazon 商品页资料快照，与用户输入分开保存。"""

from datetime import UTC, datetime
from decimal import Decimal

from pydantic import Field

from app.domain.schemas import Contract


class ProductSnapshot(Contract):
    asin: str
    title: str
    features: list[str] = Field(default_factory=list)
    description: str = ""
    image_url: str = ""
    source_url: str = ""
    price: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=4)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    # 旧快照只有已解析的一次性价格，因此默认保持可用；新解析器会显式标记不可靠报价。
    price_reliable: bool = True
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
