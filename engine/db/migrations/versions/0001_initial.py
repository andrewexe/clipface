"""Initial schema: the seven tables from PLAN.md.

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "streamers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("platform", sa.String(16), nullable=False),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("display_name", sa.String(128)),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("permission_status", sa.String(16), nullable=False),
        sa.Column("program_rules", sa.JSON()),
        sa.Column("added_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("platform", "username", name="uq_streamers_platform_username"),
        sa.CheckConstraint("platform IN ('twitch', 'kick')", name="ck_streamers_platform"),
        sa.CheckConstraint(
            "permission_status IN ('none', 'asked', 'granted', 'program')",
            name="ck_streamers_permission_status",
        ),
    )

    op.create_table(
        "sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "streamer_id",
            sa.Integer(),
            sa.ForeignKey("streamers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime()),
        sa.Column("title", sa.Text()),
        sa.Column("game", sa.String(128)),
        sa.Column("buffer_path", sa.Text()),
    )
    op.create_index("ix_sessions_streamer_id", "sessions", ["streamer_id"])

    op.create_table(
        "moments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "session_id",
            sa.Integer(),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("peak_ts", sa.DateTime(), nullable=False),
        sa.Column("signal_scores", sa.JSON()),
        sa.Column("status", sa.String(16), nullable=False),
        sa.CheckConstraint(
            "status IN ('detected', 'scored', 'rejected', 'clipped')", name="ck_moments_status"
        ),
    )
    op.create_index("ix_moments_session_id", "moments", ["session_id"])

    op.create_table(
        "clips",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "moment_id",
            sa.Integer(),
            sa.ForeignKey("moments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("start_ts", sa.DateTime(), nullable=False),
        sa.Column("end_ts", sa.DateTime(), nullable=False),
        sa.Column("file_path", sa.Text()),
        sa.Column("transcript", sa.Text()),
        sa.Column("claude_score", sa.Float()),
        sa.Column("title", sa.Text()),
        sa.Column("caption", sa.Text()),
        sa.Column("phash", sa.String(64)),
        sa.Column("policy_result", sa.JSON()),
        sa.Column("status", sa.String(16), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'published', 'removed')",
            name="ck_clips_status",
        ),
    )
    op.create_index("ix_clips_moment_id", "clips", ["moment_id"])
    op.create_index("ix_clips_status", "clips", ["status"])
    op.create_index("ix_clips_phash", "clips", ["phash"])

    op.create_table(
        "accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("platform", sa.String(16), nullable=False),
        sa.Column("handle", sa.String(128), nullable=False),
        sa.Column("keychain_ref", sa.String(128)),
        sa.Column("daily_cap", sa.Integer(), nullable=False),
        sa.Column("paused", sa.Boolean(), nullable=False),
        sa.Column("pause_reason", sa.Text()),
        sa.UniqueConstraint("platform", "handle", name="uq_accounts_platform_handle"),
        sa.CheckConstraint(
            "platform IN ('youtube', 'tiktok', 'instagram')", name="ck_accounts_platform"
        ),
    )

    op.create_table(
        "publish_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "clip_id", sa.Integer(), sa.ForeignKey("clips.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "account_id",
            sa.Integer(),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("scheduled_at", sa.DateTime(), nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("platform_post_id", sa.String(128)),
        sa.Column("error", sa.Text()),
        sa.UniqueConstraint("idempotency_key", name="uq_publish_jobs_idempotency_key"),
        sa.CheckConstraint(
            "status IN ('scheduled', 'running', 'succeeded', 'failed', 'cancelled')",
            name="ck_publish_jobs_status",
        ),
    )
    op.create_index(
        "ix_publish_jobs_status_scheduled_at", "publish_jobs", ["status", "scheduled_at"]
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ts", sa.DateTime(), nullable=False),
        sa.Column("actor", sa.String(16), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("object_id", sa.String(128)),
        sa.Column("details", sa.JSON()),
        sa.CheckConstraint("actor IN ('system', 'user', 'claude')", name="ck_audit_log_actor"),
    )
    op.create_index("ix_audit_log_ts", "audit_log", ["ts"])


def downgrade() -> None:
    for table in (
        "audit_log",
        "publish_jobs",
        "accounts",
        "clips",
        "moments",
        "sessions",
        "streamers",
    ):
        op.drop_table(table)
