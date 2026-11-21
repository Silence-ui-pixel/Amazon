"""Use faster LZ4 compression for large persisted analysis documents."""

from collections.abc import Sequence

from alembic import op

revision: str = "7ad31c0e5f42"
down_revision: str | Sequence[str] | None = "e5d7f901b3c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Apply to subsequent writes without rewriting historical evidence."""
    op.execute(
        "ALTER TABLE hot_search_core_analyses ALTER COLUMN document SET COMPRESSION lz4"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE hot_search_core_analyses ALTER COLUMN document SET COMPRESSION default"
    )
