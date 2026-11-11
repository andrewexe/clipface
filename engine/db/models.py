"""SQLAlchemy models for the seven tables in PLAN.md ("Data model")."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

PLATFORMS = ("twitch", "kick")
PERMISSION_STATUSES = ("none", "asked", "granted", "program")
MOMENT_STATUSES = ("detected", "scored", "rejected", "clipped")
CLIP_STATUSES = ("pending", "approved", "rejected", "published", "removed")
ACCOUNT_PLATFORMS = ("youtube", "tiktok", "instagram")
PUBLISH_JOB_STATUSES = ("scheduled", "running", "succeeded", "failed", "cancelled")
AUDIT_ACTORS = ("system", "user", "claude")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator):
    """Stores UTC in SQLite; refuses naive datetimes; returns aware ones."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime; pass a timezone-aware value")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        return None if value is None else value.replace(tzinfo=UTC)


class Base(DeclarativeBase):
    pass


class Streamer(Base):
    __tablename__ = "streamers"
    __table_args__ = (
        UniqueConstraint("platform", "username", name="uq_streamers_platform_username"),
        CheckConstraint(_in("platform", PLATFORMS), name="ck_streamers_platform"),
        CheckConstraint(
            _in("permission_status", PERMISSION_STATUSES), name="ck_streamers_permission_status"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[str] = mapped_column(String(16))
    username: Mapped[str] = mapped_column(String(64))
    display_name: Mapped[str | None] = mapped_column(String(128))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Auto-publish stays off unless this is "granted" or "program".
    permission_status: Mapped[str] = mapped_column(String(16), default="none")
    program_rules: Mapped[dict | None] = mapped_column(JSON)
    added_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Session(Base):
    __tablename__ = "sessions"
    __table_args__ = (Index("ix_sessions_streamer_id", "streamer_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    streamer_id: Mapped[int] = mapped_column(ForeignKey("streamers.id", ondelete="CASCADE"))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    title: Mapped[str | None] = mapped_column(Text)
    game: Mapped[str | None] = mapped_column(String(128))
    buffer_path: Mapped[str | None] = mapped_column(Text)


class Moment(Base):
    __tablename__ = "moments"
    __table_args__ = (
        Index("ix_moments_session_id", "session_id"),
        CheckConstraint(_in("status", MOMENT_STATUSES), name="ck_moments_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"))
    peak_ts: Mapped[datetime] = mapped_column(UTCDateTime)
    signal_scores: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default="detected")


class Clip(Base):
    __tablename__ = "clips"
    __table_args__ = (
        Index("ix_clips_moment_id", "moment_id"),
        Index("ix_clips_status", "status"),
        Index("ix_clips_phash", "phash"),
        CheckConstraint(_in("status", CLIP_STATUSES), name="ck_clips_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    moment_id: Mapped[int] = mapped_column(ForeignKey("moments.id", ondelete="CASCADE"))
    start_ts: Mapped[datetime] = mapped_column(UTCDateTime)
    end_ts: Mapped[datetime] = mapped_column(UTCDateTime)
    file_path: Mapped[str | None] = mapped_column(Text)
    transcript: Mapped[str | None] = mapped_column(Text)
    claude_score: Mapped[float | None] = mapped_column(Float)
    title: Mapped[str | None] = mapped_column(Text)
    caption: Mapped[str | None] = mapped_column(Text)
    phash: Mapped[str | None] = mapped_column(String(64))
    policy_result: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default="pending")


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        UniqueConstraint("platform", "handle", name="uq_accounts_platform_handle"),
        CheckConstraint(_in("platform", ACCOUNT_PLATFORMS), name="ck_accounts_platform"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[str] = mapped_column(String(16))
    handle: Mapped[str] = mapped_column(String(128))
    # Name of the keychain entry holding this account's OAuth tokens; never the tokens.
    keychain_ref: Mapped[str | None] = mapped_column(String(128))
    daily_cap: Mapped[int] = mapped_column(Integer, default=3)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    pause_reason: Mapped[str | None] = mapped_column(Text)


class PublishJob(Base):
    __tablename__ = "publish_jobs"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_publish_jobs_idempotency_key"),
        Index("ix_publish_jobs_status_scheduled_at", "status", "scheduled_at"),
        CheckConstraint(_in("status", PUBLISH_JOB_STATUSES), name="ck_publish_jobs_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    clip_id: Mapped[int] = mapped_column(ForeignKey("clips.id", ondelete="CASCADE"))
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    scheduled_at: Mapped[datetime] = mapped_column(UTCDateTime)
    idempotency_key: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="scheduled")
    platform_post_id: Mapped[str | None] = mapped_column(String(128))
    error: Mapped[str | None] = mapped_column(Text)


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_log_ts", "ts"),
        CheckConstraint(_in("actor", AUDIT_ACTORS), name="ck_audit_log_actor"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    actor: Mapped[str] = mapped_column(String(16))
    action: Mapped[str] = mapped_column(String(64))
    object_id: Mapped[str | None] = mapped_column(String(128))
    details: Mapped[dict | None] = mapped_column(JSON)
