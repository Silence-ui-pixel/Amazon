"""商品识别用例：持久化单任务结果，防止双击与重启后重复调用模型。"""

from datetime import UTC, datetime
from uuid import UUID

from app.db.repository import Repository
from app.domain.errors import DomainError
from app.domain.product_understanding import UnderstandingResult
from app.services.asin_product import AsinProductService
from app.services.images import ImageService
from app.services.qwen import QwenClient, QwenConfig


class ProductUnderstandingService:
    def __init__(
        self,
        repository: Repository,
        images: ImageService,
        client: QwenClient | None,
        asin_products: AsinProductService | None = None,
    ) -> None:
        self.repository = repository
        self.images = images
        self.client = client
        self.asin_products = asin_products or AsinProductService(repository)

    def get(self, project_id: UUID, run_id: UUID) -> UnderstandingResult:
        self.repository.get_run(project_id, run_id)
        with self.repository.connect() as conn:
            row = conn.execute(
                "SELECT document FROM product_understandings WHERE run_id = ? AND status = 'succeeded'",
                (str(run_id),),
            ).fetchone()
        if row is None:
            raise DomainError(404, "UNDERSTANDING_NOT_FOUND", "尚无已完成的商品识别结果")
        return UnderstandingResult.model_validate_json(row[0])

    def identify(self, project_id: UUID, run_id: UUID) -> UnderstandingResult:
        run = self.repository.get_run(project_id, run_id)
        # 优先返回持久化结果；更换密钥或重启服务也不会为同一任务再次付费。
        try:
            return self.get(project_id, run_id)
        except DomainError as error:
            if error.code != "UNDERSTANDING_NOT_FOUND":
                raise
        data = run.input
        if data.image_urls:
            raise DomainError(
                422, "REMOTE_IMAGE_NOT_SUPPORTED", "请先上传图片文件；远端图片地址尚未接入下载验证"
            )
        description = "\n".join(filter(None, [data.product_name, data.description]))
        resolved = None
        if data.asin:
            resolved = self.asin_products.resolve(project_id, run_id)
            description = "\n".join(
                filter(
                    None,
                    [description, "Amazon 商品页资料（待分析数据）：" + resolved.model_dump_json()],
                )
            )
        if not data.image_ids and not description:
            raise DomainError(422, "INSUFFICIENT_PRODUCT_EVIDENCE", "缺少可用于识别的商品资料")
        client = self.client or QwenClient(QwenConfig.load(self.repository.storage_root))
        client.config.validate()
        image_data: list[tuple[str, bytes]] = []
        for image_id in data.image_ids:
            asset, path = self.images.content(project_id, image_id)
            image_data.append((asset.content_type, path.read_bytes()))
        with self.repository.connect() as conn:
            row = conn.execute(
                "SELECT status, document FROM product_understandings WHERE run_id = ?",
                (str(run_id),),
            ).fetchone()
            if row and row[0] == "succeeded":
                return UnderstandingResult.model_validate_json(row[1])
            if row and row[0] in ("running", "uncertain"):
                raise DomainError(
                    409, "MODEL_OUTCOME_PENDING", "此任务正在识别或上次调用结果待确认，请勿重复调用"
                )
            conn.execute(
                "INSERT INTO product_understandings (run_id, status, document) VALUES (?, 'running', NULL) ON CONFLICT(run_id) DO UPDATE SET status='running', document=NULL",
                (str(run_id),),
            )
        try:
            product = client.understand(data.marketplace, description, image_data)
            result = UnderstandingResult(
                project_id=project_id,
                run_id=run_id,
                model=client.config.model,
                created_at=datetime.now(UTC),
                product=product,
                warnings=["模型结果为推断，请人工核对；图片用途尚未确认为商品主图。"]
                + (
                    ["商品资料来自 Amazon 详情页；主图地址已记录，本次模型依据标题和描述识别。"]
                    if resolved
                    else (["本次依据上传图片或描述识别，未核对 ASIN 详情。"] if data.asin else [])
                ),
            )
            with self.repository.connect() as conn:
                conn.execute(
                    "UPDATE product_understandings SET status='succeeded', document=? WHERE run_id=?",
                    (result.model_dump_json(), str(run_id)),
                )
            return result
        except DomainError as error:
            status = "uncertain" if error.code == "MODEL_OUTCOME_UNKNOWN" else "failed"
            with self.repository.connect() as conn:
                conn.execute(
                    "UPDATE product_understandings SET status=? WHERE run_id=?",
                    (status, str(run_id)),
                )
            raise
