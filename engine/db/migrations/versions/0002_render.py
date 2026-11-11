"""Rendering: per-streamer layout, clip details, longer hashes, spend ledger.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("streamers") as batch:
        # Where the facecam is in this streamer's layout, set by dragging in the UI.
        batch.add_column(sa.Column("layout", sa.JSON()))
    with op.batch_alter_table("clips") as batch:
        batch.add_column(sa.Column("meta", sa.JSON()))
        # One hash per sampled frame, so it no longer fits in 64 characters.
        batch.alter_column("phash", type_=sa.Text(), existing_type=sa.String(64))
    op.create_table(
        "spend",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ts", sa.DateTime(), nullable=False),
        sa.Column("service", sa.String(32), nullable=False),
        sa.Column("usd", sa.Float(), nullable=False),
        sa.Column("details", sa.JSON()),
    )
    op.create_index("ix_spend_service_ts", "spend", ["service", "ts"])


def downgrade() -> None:
    op.drop_table("spend")
    with op.batch_alter_table("clips") as batch:
        batch.alter_column("phash", type_=sa.String(64), existing_type=sa.Text())
        batch.drop_column("meta")
    with op.batch_alter_table("streamers") as batch:
        batch.drop_column("layout")
