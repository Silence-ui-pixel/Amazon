"""追加式商品纠错确认；原始模型识别结果保持不变。"""

from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from app.db.repository import Repository
from app.domain.errors import DomainError
from app.domain.product_understanding import ProductConfirmation, ProductConfirmationRequest
from app.services.product_understanding import ProductUnderstandingService


class ProductConfirmationService:
    def __init__(self, repository: Repository, understanding: ProductUnderstandingService) -> None:
        self.repository = repository
        self.understanding = understanding

    def history(self, project: UUID, run: UUID) -> list[ProductConfirmation]:
        self.repository.get_run(project, run)
        with self.repository.connect() as conn:
            rows = conn.execute(
                "SELECT document FROM product_confirmations WHERE run_id=? ORDER BY version DESC",
                (str(run),),
            ).fetchall()
        return [ProductConfirmation.model_validate_json(row[0]) for row in rows]

    def get(self, project: UUID, run: UUID) -> ProductConfirmation:
        history = self.history(project, run)
        if not history:
            raise DomainError(404, "PRODUCT_CONFIRMATION_NOT_FOUND", "尚未人工确认商品识别结果")
        return history[0]

    def save(
        self, project: UUID, run: UUID, data: ProductConfirmationRequest
    ) -> ProductConfirmation:
        self.understanding.get(project, run)
        with self.repository.connect() as conn:
            if conn.execute(
                "SELECT 1 FROM root_candidates WHERE run_id=? AND status IN ('running','uncertain','succeeded')",
                (str(run),),
            ).fetchone():
                raise DomainError(
                    409,
                    "PRODUCT_CONFIRMATION_LOCKED",
                    "已生成或正在生成词根，请新建研究修改商品资料，避免旧流程证据错配",
                )
            version_row = conn.execute(
                "SELECT COALESCE(MAX(version),0) FROM product_confirmations WHERE run_id=?",
                (str(run),),
            ).fetchone()
            assert version_row is not None
            version = cast(int, version_row[0])
            if version != data.expected_version:
                raise DomainError(
                    409, "PRODUCT_CONFIRMATION_CONFLICT", "商品确认已更新，请读取最新记录后重试"
                )
            result = ProductConfirmation(
                project_id=project,
                run_id=run,
                version=version + 1,
                created_at=datetime.now(UTC),
                data=data,
            )
            conn.execute(
                "INSERT INTO product_confirmations VALUES(?,?,?)",
                (str(run), result.version, result.model_dump_json()),
            )
        return result
