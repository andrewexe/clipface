"""Review queue: list clips, preview, edit, approve or reject, export."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from engine.api.app import AppContext, get_ctx
from engine.control.autonomy import demote_to_l2
from engine.db.audit import write_audit
from engine.db.models import Clip, Moment, PublishJob, Streamer, StreamSession, utcnow
from engine.monitor.urls import StreamerRef
from engine.policy.rules import build_description, credit_line
from engine.publish.export import ExportError, ExportItem, export_clip
from engine.render.render import RenderError

log = logging.getLogger(__name__)
router = APIRouter()

TITLE_MAX = 100  # YouTube's limit, the tightest of the platforms we target


class ClipPatch(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=TITLE_MAX)
    caption: str | None = Field(None, max_length=2000)


def _row(db: Session, clip_id: int) -> tuple[Clip, Moment, Streamer]:
    row = db.execute(
        select(Clip, Moment, Streamer)
        .join(Moment, Moment.id == Clip.moment_id)
        .join(StreamSession, StreamSession.id == Moment.session_id)
        .join(Streamer, Streamer.id == StreamSession.streamer_id)
        .where(Clip.id == clip_id)
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="clip not found")
    return row


def _view(clip: Clip, moment: Moment, streamer: Streamer) -> dict:
    ref = StreamerRef(streamer.platform, streamer.username)
    name = streamer.display_name or streamer.username
    meta = clip.meta or {}
    return {
        "id": clip.id,
        "status": clip.status,
        "title": clip.title,
        "caption": clip.caption,
        "transcript": clip.transcript,
        "score": clip.claude_score,
        "start_ts": clip.start_ts.isoformat(),
        "duration": meta.get("duration") or (clip.end_ts - clip.start_ts).total_seconds(),
        "streamer": {
            "id": streamer.id,
            "name": name,
            "platform": streamer.platform,
            "url": ref.url,
            "permission_status": streamer.permission_status,
        },
        "credit": credit_line(name, ref.url, streamer.program_rules),
        "description": build_description(clip.caption, name, ref.url, streamer.program_rules),
        "triggers": (moment.signal_scores or {}).get("triggers", []),
        "signal_scores": moment.signal_scores,
        "policy": clip.policy_result,
        "meta": meta,
        "has_video": bool(clip.file_path) and Path(clip.file_path).exists(),
    }


def _export_dir(ctx: AppContext) -> Path:
    custom = ctx.settings.get().export_dir.strip()
    return Path(custom).expanduser() if custom else ctx.config.default_export_dir


def _export(
    ctx: AppContext, db: Session, clip: Clip, streamer: Streamer, actor: str = "user"
) -> Path:
    name = streamer.display_name or streamer.username
    target = export_clip(
        ExportItem(
            clip_id=clip.id,
            video=Path(clip.file_path or ""),
            title=clip.title or f"{name} clip",
            caption=clip.caption,
            streamer_name=name,
            channel_url=StreamerRef(streamer.platform, streamer.username).url,
            rules=streamer.program_rules,
        ),
        _export_dir(ctx),
    )
    clip.meta = {**(clip.meta or {}), "exported_path": str(target)}
    write_audit(
        db, actor=actor, action="clip.export", object_id=f"clip:{clip.id}",
        details={"path": str(target)},
    )  # fmt: skip
    return target


@router.get("/clips")
def list_clips(
    status: Literal["pending", "approved", "rejected", "published", "removed"] = "pending",
    limit: int = Query(100, ge=1, le=500),
    ctx: AppContext = Depends(get_ctx),
) -> dict:
    with ctx.session_factory() as db:
        rows = db.execute(
            select(Clip, Moment, Streamer)
            .join(Moment, Moment.id == Clip.moment_id)
            .join(StreamSession, StreamSession.id == Moment.session_id)
            .join(Streamer, Streamer.id == StreamSession.streamer_id)
            .where(Clip.status == status)
            .order_by(Clip.id if status == "pending" else Clip.id.desc())
            .limit(limit)
        ).all()
        counts = dict(db.execute(select(Clip.status, func.count()).group_by(Clip.status)).all())
        clips = [_view(*row) for row in rows]
    return {
        "clips": clips,
        "counts": counts,
        "spot_checks": len(_spot_check_rows(ctx)),
        "export_dir": str(_export_dir(ctx)),
    }


def _spot_check_rows(ctx: AppContext) -> list[dict]:
    """Clips that were published without review at L3 and picked for a look afterwards."""
    with ctx.session_factory() as db:
        rows = db.execute(
            select(Clip, Moment, Streamer)
            .join(Moment, Moment.id == Clip.moment_id)
            .join(StreamSession, StreamSession.id == Moment.session_id)
            .join(Streamer, Streamer.id == StreamSession.streamer_id)
            .where(Clip.status.in_(("approved", "published")))
            .order_by(Clip.id)
        ).all()
        return [_view(*row) for row in rows if (row[0].meta or {}).get("spot_check") == "pending"]


class SpotCheckIn(BaseModel):
    ok: bool


@router.get("/clips/spot-checks")
def list_spot_checks(ctx: AppContext = Depends(get_ctx)) -> dict:
    return {"clips": _spot_check_rows(ctx)}


@router.post("/clips/{clip_id}/spot-check")
def answer_spot_check(clip_id: int, body: SpotCheckIn, ctx: AppContext = Depends(get_ctx)) -> dict:
    """Your verdict on a clip that was published before you saw it. "Not ok" takes it
    down where the platform allows, cancels its waiting posts, and drops autonomy to L2."""
    with ctx.session_factory() as db:
        clip, _moment, _streamer = _row(db, clip_id)
        if (clip.meta or {}).get("spot_check") != "pending":
            raise HTTPException(
                status_code=409, detail="This clip is not waiting for a spot check."
            )
        was_published = clip.status == "published"
    taken_down = True
    if not body.ok and was_published:
        # Network calls: outside any database transaction.
        taken_down = bool(ctx.remove_published and ctx.remove_published(clip_id))
    with ctx.session_factory.begin() as db:
        clip, moment, streamer = _row(db, clip_id)
        meta = dict(clip.meta or {})
        meta["spot_check"] = "passed" if body.ok else "failed"
        if not body.ok:
            for job in db.scalars(
                select(PublishJob).where(
                    PublishJob.clip_id == clip_id, PublishJob.status.in_(("scheduled", "failed"))
                )
            ):
                job.status = "cancelled"
            if not was_published:
                clip.status = "rejected"
            elif taken_down:
                clip.status = "removed"
            else:
                meta["remove_by_hand"] = True  # still up: the platform cannot delete it for us
        clip.meta = meta
        write_audit(
            db,
            actor="user",
            action="clip.spot_check",
            object_id=f"clip:{clip_id}",
            details={"ok": body.ok, "taken_down": taken_down if not body.ok else None},
        )
        db.flush()
        view = _view(clip, moment, streamer)
    if not body.ok:
        demoted = demote_to_l2(
            ctx.settings, ctx.session_factory, f"clip {clip_id} failed its spot check"
        )
        view["taken_down"] = taken_down
        view["autonomy_dropped"] = demoted
        if ctx.publisher is not None:
            ctx.publisher.wake()
    return view


@router.get("/clips/{clip_id}")
def get_clip(clip_id: int, ctx: AppContext = Depends(get_ctx)) -> dict:
    with ctx.session_factory() as db:
        return _view(*_row(db, clip_id))


def _file(ctx: AppContext, clip_id: int, suffix: str, media_type: str) -> FileResponse:
    with ctx.session_factory() as db:
        clip, _moment, _streamer = _row(db, clip_id)
        path = Path(clip.file_path or "").with_suffix(suffix)
    # Only ever serve files the engine itself rendered into its clips folder.
    if not path.is_file() or path.parent.resolve() != ctx.config.clips_dir.resolve():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(path, media_type=media_type)


@router.get("/clips/{clip_id}/video")
def clip_video(clip_id: int, ctx: AppContext = Depends(get_ctx)) -> FileResponse:
    return _file(ctx, clip_id, ".mp4", "video/mp4")


@router.get("/clips/{clip_id}/thumbnail")
def clip_thumbnail(clip_id: int, ctx: AppContext = Depends(get_ctx)) -> FileResponse:
    return _file(ctx, clip_id, ".jpg", "image/jpeg")


@router.patch("/clips/{clip_id}")
def edit_clip(clip_id: int, body: ClipPatch, ctx: AppContext = Depends(get_ctx)) -> dict:
    with ctx.session_factory.begin() as db:
        clip, moment, streamer = _row(db, clip_id)
        changes = {}
        if body.title is not None and body.title.strip() != (clip.title or ""):
            title = " ".join(body.title.split())
            if not title:
                raise HTTPException(status_code=422, detail="The title can't be empty.")
            changes["title"] = {"from": clip.title, "to": title}
            clip.title = title
        if body.caption is not None and body.caption.strip() != (clip.caption or ""):
            changes["caption"] = True
            clip.caption = body.caption.strip() or None
        if changes:
            write_audit(
                db, actor="user", action="clip.edit", object_id=f"clip:{clip.id}", details=changes
            )
        db.flush()
        return _view(clip, moment, streamer)


_ACTIONS = {"approved": "clip.approve", "rejected": "clip.reject", "pending": "clip.restore"}


def _set_status(
    ctx: AppContext,
    clip_id: int,
    new: str,
    allowed_from: tuple[str, ...],
    via: str | None = None,
    actor: str = "user",
) -> dict:
    with ctx.session_factory.begin() as db:
        clip, moment, streamer = _row(db, clip_id)
        if clip.status == new:
            return _view(clip, moment, streamer)
        if clip.status not in allowed_from:
            raise HTTPException(
                status_code=409, detail=f"A {clip.status} clip can't be changed to {new}."
            )
        write_audit(
            db,
            actor=actor,
            action=_ACTIONS[new],
            object_id=f"clip:{clip.id}",
            details={"from": clip.status, **({"via": via} if via else {})},
        )
        clip.status = new
        clip.meta = {**(clip.meta or {}), "reviewed_at": utcnow().isoformat()}
        view = None
        export_error = None
        if new == "approved":
            try:
                _export(ctx, db, clip, streamer, actor)
            except ExportError as exc:
                export_error = str(exc)
                log.warning("export of clip %s failed: %s", clip.id, exc)
        db.flush()
        view = _view(clip, moment, streamer)
        view["export_error"] = export_error
    if new == "approved" and ctx.publisher is not None:
        view["scheduled_posts"] = len(ctx.publisher.schedule_clip(clip_id))
    return view


@router.post("/clips/{clip_id}/approve")
def approve_clip(clip_id: int, ctx: AppContext = Depends(get_ctx)) -> dict:
    """Approve and copy to the export folder for manual upload."""
    return _set_status(ctx, clip_id, "approved", ("pending", "rejected"))


@router.post("/clips/{clip_id}/reject")
def reject_clip(clip_id: int, ctx: AppContext = Depends(get_ctx)) -> dict:
    return _set_status(ctx, clip_id, "rejected", ("pending", "approved"))


@router.post("/clips/{clip_id}/restore")
def restore_clip(clip_id: int, ctx: AppContext = Depends(get_ctx)) -> dict:
    """Put a reviewed clip back in the queue."""
    return _set_status(ctx, clip_id, "pending", ("approved", "rejected"))


@router.post("/clips/{clip_id}/export")
def export_one(clip_id: int, ctx: AppContext = Depends(get_ctx)) -> dict:
    with ctx.session_factory.begin() as db:
        clip, moment, streamer = _row(db, clip_id)
        if clip.status != "approved":
            raise HTTPException(status_code=409, detail="Only approved clips can be exported.")
        try:
            _export(ctx, db, clip, streamer)
        except ExportError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from None
        db.flush()
        return _view(clip, moment, streamer)


@router.post("/clips/{clip_id}/rerender")
def rerender_clip(clip_id: int, ctx: AppContext = Depends(get_ctx)) -> dict:
    """Render again with the streamer's current layout (after moving the facecam box)."""
    if ctx.pipeline is None:
        raise HTTPException(status_code=503, detail="The renderer is not running.")
    with ctx.session_factory() as db:
        clip, _moment, _streamer = _row(db, clip_id)
        if clip.status not in ("pending", "approved"):
            raise HTTPException(
                status_code=409, detail=f"A {clip.status} clip can't be re-rendered."
            )
    try:
        ctx.pipeline.rerender(clip_id)
    except RenderError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    with ctx.session_factory() as db:
        return _view(*_row(db, clip_id))
