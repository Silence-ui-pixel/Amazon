"""Persist resumable model traffic review jobs."""

import sqlalchemy as sa
from alembic import op

revision = "f6a8b1c2d3e4"
down_revision = "c3a0f8d41b62"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "traffic_review_jobs",
        sa.Column("analysis_id", sa.Text(), sa.ForeignKey("traffic_analyses.id"), primary_key=True),
        sa.Column("run_id", sa.Text(), sa.ForeignKey("pipeline_runs.id"), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("document", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("traffic_review_jobs")
