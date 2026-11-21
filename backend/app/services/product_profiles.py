"""本品四维基准提取、可靠价格选择和追加式确认。"""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from app.db.repository import Repository
from app.domain.asin_product import ProductSnapshot
from app.domain.errors import DomainError
from app.domain.product_terms import ProductProfile, ProductProfileRecord, ProductProfileRequest
from app.services.asin_product import AsinProductService
from app.services.product_confirmation import ProductConfirmationService
from app.services.qwen import QwenClient, QwenConfig


def resolve_profile_price(
    profile: ProductProfile,
    snapshot: ProductSnapshot | None,
    reference_price: Decimal | None,
    currency: str,
) -> ProductProfile:
    """ASIN 可靠当前价优先；图片和不可靠报价只能使用用户确认的参考售价。"""
    if snapshot and snapshot.price is not None and snapshot.currency and snapshot.price_reliable:
        return profile.model_copy(
            update={
                "price": snapshot.price,
                "currency": snapshot.currency,
                "price_source": "asin_current",
            }
        )
    if reference_price is not None:
        return profile.model_copy(
            update={
                "price": reference_price,
                "currency": currency,
                "price_source": "user_reference",
            }
        )
    raise ValueError("纯图片或 ASIN 缺少可靠价格时，必须填写并确认参考售价")


class ProductProfileService:
    def __init__(
        self,
        repository: Repository,
        confirmations: ProductConfirmationService,
        asin_products: AsinProductService,
        client: QwenClient | None,
    ) -> None:
        self.repository = repository
        self.confirmations = confirmations
        self.asin_products = asin_products
        self.client = client

    def history(self, project: UUID, run: UUID) -> list[ProductProfileRecord]:
        self.repository.get_run(project, run)
        with self.repository.connect() as conn:
            rows = conn.execute(
                "SELECT document FROM product_profiles WHERE run_id=? ORDER BY version DESC",
                (str(run),),
            ).fetchall()
        return [ProductProfileRecord.model_validate_json(row[0]) for row in rows]

    def get(self, project: UUID, run: UUID) -> ProductProfileRecord:
        records = self.history(project, run)
        if not records:
            raise DomainError(404, "PRODUCT_PROFILE_NOT_FOUND", "尚未生成商品四维基准")
        return records[0]

    def generate(
        self,
        project: UUID,
        run: UUID,
        reference_price: Decimal | None = None,
        currency: str = "USD",
    ) -> ProductProfileRecord:
        existing = self.history(project, run)
        if existing:
            return existing[0]
        confirmed = self.confirmations.get(project, run)
        pipeline = self.repository.get_run(project, run)
        snapshot = (
            self.asin_products.resolve(project, run, refresh_missing_price=True)
            if pipeline.input.asin
            else None
        )
        client = self.client or QwenClient(QwenConfig.load(self.repository.storage_root))
        draft = client.extract_product_profile(confirmed.data.product, snapshot)
        try:
            profile = resolve_profile_price(
                draft,
                snapshot,
                reference_price if reference_price is not None else pipeline.input.reference_price,
                currency if reference_price is not None else pipeline.input.reference_currency,
            )
        except ValueError as error:
            raise DomainError(422, "REFERENCE_PRICE_REQUIRED", str(error)) from None
        record = ProductProfileRecord(
            project_id=project, run_id=run, version=1, status="draft", profile=profile
        )
        with self.repository.connect() as conn:
            conn.execute(
                "INSERT INTO product_profiles (run_id, version, status, document) VALUES(?,?,?,?) ON CONFLICT (run_id, version) DO NOTHING",
                (str(run), 1, "draft", record.model_dump_json()),
            )
        return self.get(project, run)

    def confirm(
        self, project: UUID, run: UUID, data: ProductProfileRequest
    ) -> ProductProfileRecord:
        pipeline = self.repository.get_run(project, run)
        snapshot = self.asin_products.resolve(project, run) if pipeline.input.asin else None
        base = ProductProfile(
            asin=pipeline.input.asin or "",
            purpose=data.purpose,
            style_structure=data.style_structure,
            specifications=data.specifications,
            package_quantity=data.package_quantity,
            unit_basis=data.unit_basis,
        )
        try:
            profile = resolve_profile_price(base, snapshot, data.reference_price, data.currency)
        except ValueError as error:
            raise DomainError(422, "REFERENCE_PRICE_REQUIRED", str(error)) from None
        with self.repository.connect() as conn:
            conn.execute("SELECT id FROM pipeline_runs WHERE id=? FOR NO KEY UPDATE", (str(run),))
            if conn.execute("SELECT 1 FROM product_term_analyses WHERE run_id=? AND document::jsonb->>'status' IN ('queued','running') LIMIT 1", (str(run),)).fetchone():
                raise DomainError(409, "PRODUCT_TERM_ANALYSIS_RUNNING", "商品词分析正在运行，请完成后再修改四维基准")
            row = conn.execute(
                "SELECT version, document FROM product_profiles WHERE run_id=? ORDER BY version DESC LIMIT 1",
                (str(run),),
            ).fetchone()
            version = int(row[0]) if row else 0
            if version != data.expected_version:
                if row:
                    latest = ProductProfileRecord.model_validate_json(row[1])
                    if latest.status == "confirmed" and latest.profile == profile:
                        return latest
                raise DomainError(
                    409, "PRODUCT_PROFILE_CONFLICT", "商品四维基准已更新，请读取最新版本后重试"
                )
            record = ProductProfileRecord(
                project_id=project,
                run_id=run,
                version=version + 1,
                status="confirmed",
                profile=profile,
                created_at=datetime.now(UTC),
            )
            conn.execute(
                "INSERT INTO product_profiles VALUES(?,?,?,?)",
                (str(run), record.version, record.status, record.model_dump_json()),
            )
            # 新基准版本使自动分析与自动选择失效；历史分析记录仍可读取。
            conn.execute(
                "DELETE FROM root_selections WHERE run_id=? AND CAST(document AS jsonb) ->> 'source' = 'automatic'",
                (str(run),),
            )
            conn.execute(
                "UPDATE product_term_analyses SET document=jsonb_set(jsonb_set(CAST(document AS jsonb), '{status}', to_jsonb('stale'::text)), '{complete}', to_jsonb(false))::text WHERE run_id=?",
                (str(run),),
            )
        return record
