"""Add small durable progress rows for hot-search analyses."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3a0f8d41b62"
down_revision: str | Sequence[str] | None = "7ad31c0e5f42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "hot_search_core_analysis_journal",
        sa.Column("analysis_id", sa.Text(), nullable=False),
        sa.Column("batch_id", sa.Text(), nullable=False),
        sa.Column("candidate_id", sa.Text(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("document", sa.Text(), nullable=False),
        sa.Column("seq", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.ForeignKeyConstraint(
            ["analysis_id"],
            ["hot_search_core_analyses.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("analysis_id", "batch_id"),
    )
    op.create_index(
        "ix_hot_search_core_analysis_journal_candidate",
        "hot_search_core_analysis_journal",
        ["analysis_id", "candidate_id"],
    )
    op.create_index(
        "ix_hot_search_core_analysis_journal_kind",
        "hot_search_core_analysis_journal",
        ["analysis_id", "kind"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_hot_search_core_analysis_journal_kind",
        table_name="hot_search_core_analysis_journal",
    )
    op.drop_index(
        "ix_hot_search_core_analysis_journal_candidate",
        table_name="hot_search_core_analysis_journal",
    )
    op.drop_table("hot_search_core_analysis_journal")
