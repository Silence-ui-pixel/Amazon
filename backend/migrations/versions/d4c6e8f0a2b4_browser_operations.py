"""add durable browser operations

Revision ID: d4c6e8f0a2b4
Revises: ab7db2d9bcf7
Create Date: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4c6e8f0a2b4"
down_revision: str | Sequence[str] | None = "ab7db2d9bcf7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "browser_operations",
        sa.Column("operation_id", sa.Text(), nullable=False),
        sa.Column("workflow_id", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("node_key", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("auto_resume_remaining", sa.Integer(), nullable=False),
        sa.Column("request_summary", sa.Text(), nullable=False),
        sa.Column("result_ref", sa.Text(), nullable=False),
        sa.Column("error_code", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["pipeline_runs.id"]),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflow_runs.workflow_id"]),
        sa.PrimaryKeyConstraint("operation_id"),
        sa.UniqueConstraint("run_id", "node_key", "source", "idempotency_key"),
    )
    op.create_index(
        "browser_operations_waiting",
        "browser_operations",
        ["status", "updated_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("browser_operations_waiting", table_name="browser_operations")
    op.drop_table("browser_operations")
