"""Publishing: per-job state (upload session, attempts) and post metrics.

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("publish_jobs") as batch:
        batch.add_column(sa.Column("state", sa.JSON()))
    op.create_table(
        "metrics",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "job_id",
            sa.Integer(),
            sa.ForeignKey("publish_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("post_id", sa.String(128), nullable=False),
        sa.Column("checked_at", sa.DateTime(), nullable=False),
        sa.Column("views", sa.Integer(), nullable=False),
        sa.Column("likes", sa.Integer(), nullable=False),
        sa.Column("comments", sa.Integer(), nullable=False),
    )
    op.create_index("ix_metrics_job_id_checked_at", "metrics", ["job_id", "checked_at"])


def downgrade() -> None:
    op.drop_table("metrics")
    with op.batch_alter_table("publish_jobs") as batch:
        batch.drop_column("state")
