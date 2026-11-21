"""initial schema

Revision ID: ab7db2d9bcf7
Revises:
Create Date: 2026-10-30 17:00:14.429117

初始迁移：按 app.db.models 的元数据创建全部 31 张业务/工作流表。
LangGraph checkpointer 的表（checkpoints 等）由 PostgresSaver.setup() 负责，不在此处创建。
"""

from collections.abc import Sequence

from alembic import op

from app.db.models import metadata

# revision identifiers, used by Alembic.
revision: str = "ab7db2d9bcf7"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建全部业务表与工作流投影表。"""
    metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    """删除全部表。"""
    metadata.drop_all(bind=op.get_bind())
