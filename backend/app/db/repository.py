"""业务持久化门面：基于 SQLAlchemy 同步引擎，PostgreSQL 已替换 SQLite。

``Repository.connect()`` 返回一个提交/回滚上下文，替代原 SQLite 的
``with closing(connect()) as conn, conn:`` 两段式写法。SQL 字符串沿用 ``?``
占位符，由门面统一转成 SQLAlchemy 命名参数；方言差异（rowid、INSERT OR IGNORE、
json_* 等）在各 service 中已翻译为 PostgreSQL 语法。
"""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import Engine

from app.db.scope import ConnectionScope
from app.domain.errors import DomainError
from app.domain.schemas import ImageAsset, PipelineRun, ProductInput, Project, ProjectCreate


class Repository:
    """封装数据库访问；业务库、工作流投影与 checkpointer 共用同一 PostgreSQL 库。"""

    def __init__(self, engine: Engine, storage_root: Path) -> None:
        self.engine = engine
        self.storage_root = storage_root

    def initialize(self) -> None:
        """启动时仅校验连通性；schema 由 Alembic 迁移显式创建。"""
        with self.connect() as conn:
            conn.execute("SELECT 1")

    def connect(self) -> ConnectionScope:
        return ConnectionScope(self.engine)

    def ready(self) -> bool:
        """校验业务表是否已通过迁移创建。"""
        with self.connect() as conn:
            row = conn.execute("SELECT to_regclass('projects') IS NOT NULL").fetchone()
            return bool(row is not None and row[0])

    def create_project(self, data: ProjectCreate) -> Project:
        project = Project(id=uuid4(), created_at=datetime.now(UTC), **data.model_dump())
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO projects (id, document) VALUES (?, ?)",
                (str(project.id), project.model_dump_json()),
            )
        return project

    def get_project(self, project_id: UUID) -> Project:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT document FROM projects WHERE id = ?", (str(project_id),)
            ).fetchone()
        if row is None:
            raise DomainError(404, "NOT_FOUND", "项目不存在")
        return Project.model_validate_json(cast(str, row[0]))

    def create_run(self, project_id: UUID, data: ProductInput, key: str) -> PipelineRun:
        """同项目、同幂等键、同输入复用原任务；同键不同输入返回冲突。"""
        project = self.get_project(project_id)
        if data.marketplace != project.marketplace:
            raise DomainError(422, "MARKETPLACE_MISMATCH", "任务站点必须与项目站点一致")
        for image_id in data.image_ids:
            self.get_image(project_id, image_id)
        canonical = json.dumps(data.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
        fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        run = PipelineRun(
            id=uuid4(), project_id=project_id, input=data, created_at=datetime.now(UTC)
        )
        with self.connect() as conn:
            # 幂等插入：唯一约束 (project_id, idempotency_key) 是并发仲裁者。
            conn.execute(
                "INSERT INTO pipeline_runs (id, project_id, idempotency_key, input_hash, document) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT (project_id, idempotency_key) DO NOTHING",
                (str(run.id), str(project_id), key, fingerprint, run.model_dump_json()),
            )
            row = conn.execute(
                "SELECT input_hash, document FROM pipeline_runs "
                "WHERE project_id = ? AND idempotency_key = ?",
                (str(project_id), key),
            ).fetchone()
            assert row is not None
            if row[0] != fingerprint:
                raise DomainError(409, "IDEMPOTENCY_CONFLICT", "此幂等键已用于不同的输入")
            return PipelineRun.model_validate_json(cast(str, row[1]))

    def get_run(self, project_id: UUID, run_id: UUID) -> PipelineRun:
        """同时匹配项目与任务 ID，避免从其他项目路径读到该任务。"""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT document FROM pipeline_runs WHERE id = ? AND project_id = ?",
                (str(run_id), str(project_id)),
            ).fetchone()
        if row is None:
            raise DomainError(404, "NOT_FOUND", "此项目下未找到该任务")
        return PipelineRun.model_validate_json(cast(str, row[0]))

    def get_image(self, project_id: UUID, asset_id: UUID) -> ImageAsset:
        """按项目归属查询图片；该检查不代替未来的用户权限控制。"""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT document FROM images WHERE id = ? AND project_id = ?",
                (str(asset_id), str(project_id)),
            ).fetchone()
        if row is None:
            raise DomainError(404, "NOT_FOUND", "此项目下未找到该图片")
        return ImageAsset.model_validate_json(cast(str, row[0]))

    def create_image(self, asset: ImageAsset, key: str, fingerprint: str) -> ImageAsset:
        """事务内幂等创建，重试复用同一张图片记录。"""
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO images (id, project_id, idempotency_key, fingerprint, document) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT (project_id, idempotency_key) DO NOTHING",
                (str(asset.id), str(asset.project_id), key, fingerprint, asset.model_dump_json()),
            )
            row = conn.execute(
                "SELECT fingerprint, document FROM images WHERE project_id = ? AND idempotency_key = ?",
                (str(asset.project_id), key),
            ).fetchone()
            assert row is not None
            if row[0] != fingerprint:
                raise DomainError(409, "IDEMPOTENCY_CONFLICT", "此上传幂等键已用于不同图片")
            return ImageAsset.model_validate_json(cast(str, row[1]))
