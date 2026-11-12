"""When a streamer asks you to stop: one action that stops everything for them."""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from engine.db.audit import write_audit
from engine.db.models import Clip, Moment, PublishJob, Streamer, StreamSession, utcnow
from engine.monitor.service import close_open_sessions


def stop_streamer(
    db: Session, streamer: Streamer, remove_published: Callable[[int], bool] | None = None
) -> dict:
    """Disable the streamer, withdraw permission, drop their unpublished clips, cancel
    scheduled posts, and take down what was published. Returns what was done.

    `remove_published(clip_id)` deletes a clip's posts from the platforms and returns
    whether it succeeded; clips it could not remove are reported so you can do it by hand.
    """
    streamer.enabled = False
    streamer.permission_status = "none"
    close_open_sessions(db, streamer.id, utcnow(), "streamer asked to stop")

    clips = db.scalars(
        select(Clip)
        .join(Moment, Moment.id == Clip.moment_id)
        .join(StreamSession, StreamSession.id == Moment.session_id)
        .where(StreamSession.streamer_id == streamer.id)
    ).all()
    summary = {"dropped": 0, "removed": 0, "could_not_remove": [], "cancelled_posts": 0}
    for clip in clips:
        jobs = db.scalars(
            select(PublishJob).where(
                PublishJob.clip_id == clip.id, PublishJob.status.in_(("scheduled", "failed"))
            )
        ).all()
        for job in jobs:
            job.status = "cancelled"
            summary["cancelled_posts"] += 1
        if clip.status in ("pending", "approved"):
            clip.status = "rejected"
            clip.meta = {**(clip.meta or {}), "stopped": "the streamer asked to stop"}
            summary["dropped"] += 1
        elif clip.status == "published":
            if remove_published is not None and remove_published(clip.id):
                clip.status = "removed"
                summary["removed"] += 1
            else:
                summary["could_not_remove"].append(clip.id)
    write_audit(
        db,
        actor="user",
        action="streamer.stop",
        object_id=f"streamer:{streamer.id}",
        details=summary,
    )
    return summary
