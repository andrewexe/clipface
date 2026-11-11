"""Streamers: add by URL, list with live status, enable/disable, remove."""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from engine.api.app import AppContext, get_ctx
from engine.db.audit import write_audit
from engine.db.models import Streamer, StreamSession, utcnow
from engine.media.ffmpeg import ffmpeg_path, no_window_kwargs
from engine.monitor.service import StreamerStatus, close_open_sessions
from engine.monitor.urls import MAX_INPUT_LEN, InvalidStreamerUrl, StreamerRef, parse_streamer_url
from engine.render.layout import valid_facecam

log = logging.getLogger(__name__)
router = APIRouter()

PermissionStatus = Literal["none", "asked", "granted", "program"]


class StreamerIn(BaseModel):
    url: str = Field(max_length=MAX_INPUT_LEN)


class Facecam(BaseModel):
    x: float = Field(ge=0, lt=1)
    y: float = Field(ge=0, lt=1)
    w: float = Field(ge=0.05, le=1)
    h: float = Field(ge=0.05, le=1)


class LayoutIn(BaseModel):
    facecam: Facecam | None = None  # null: no facecam, use the blurred-background layout


class StreamerPatch(BaseModel):
    enabled: bool | None = None
    permission_status: PermissionStatus | None = None
    layout: LayoutIn | None = None


def _view(
    streamer: Streamer,
    live: StreamSession | None,
    status: StreamerStatus,
    recording: dict | None = None,
) -> dict:
    if live is not None:
        state = "live"
    elif status.exists is False:
        state = "not_found"
    elif not streamer.enabled:
        state = "paused"
    elif status.last_checked_at is None:
        state = "checking"
    else:
        state = "offline"
    return {
        "id": streamer.id,
        "platform": streamer.platform,
        "username": streamer.username,
        "display_name": streamer.display_name,
        "url": StreamerRef(streamer.platform, streamer.username).url,
        "enabled": streamer.enabled,
        "permission_status": streamer.permission_status,
        "facecam": valid_facecam(streamer.layout),
        "added_at": streamer.added_at.isoformat(),
        "state": state,
        "last_checked_at": status.last_checked_at.isoformat() if status.last_checked_at else None,
        "session": None
        if live is None
        else {
            "id": live.id,
            "started_at": live.started_at.isoformat(),
            "title": live.title,
            "game": live.game,
            "viewer_count": status.viewer_count,
            "recording": recording,
        },
    }


def _status(ctx: AppContext, streamer_id: int) -> StreamerStatus:
    return ctx.monitor.streamer_status(streamer_id) if ctx.monitor else StreamerStatus()


def _open_session(db: Session, streamer_id: int) -> StreamSession | None:
    return db.scalars(
        select(StreamSession)
        .where(StreamSession.streamer_id == streamer_id, StreamSession.ended_at.is_(None))
        .order_by(StreamSession.started_at.desc())
    ).first()


def _get_or_404(db: Session, streamer_id: int) -> Streamer:
    streamer = db.get(Streamer, streamer_id)
    if streamer is None:
        raise HTTPException(status_code=404, detail="streamer not found")
    return streamer


@router.get("/streamers")
def list_streamers(ctx: AppContext = Depends(get_ctx)) -> dict:
    with ctx.session_factory() as db:
        streamers = db.scalars(select(Streamer).order_by(Streamer.added_at, Streamer.id)).all()
        items = []
        for streamer in streamers:
            live = _open_session(db, streamer.id)
            recording = ctx.recorder.status(live.id) if ctx.recorder and live else None
            items.append(_view(streamer, live, _status(ctx, streamer.id), recording))
    return {
        "recorder": ctx.recorder.summary() if ctx.recorder else None,
        "streamers": items,
        "platforms": ctx.monitor.platform_status() if ctx.monitor else {},
        "poll_interval_s": ctx.monitor.poll_interval_s if ctx.monitor else None,
    }


@router.post("/streamers", status_code=201)
def add_streamer(body: StreamerIn, ctx: AppContext = Depends(get_ctx)) -> dict:
    try:
        ref = parse_streamer_url(body.url)
    except InvalidStreamerUrl as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    try:
        with ctx.session_factory.begin() as db:
            streamer = Streamer(platform=ref.platform, username=ref.username)
            db.add(streamer)
            db.flush()
            write_audit(
                db,
                actor="user",
                action="streamer.add",
                object_id=f"streamer:{streamer.id}",
                details={"platform": ref.platform, "username": ref.username},
            )
            view = _view(streamer, None, StreamerStatus())
    except IntegrityError:
        raise HTTPException(status_code=409, detail="You already follow that channel.") from None
    log.info("streamer added: %s/%s", ref.platform, ref.username)
    if ctx.monitor:
        ctx.monitor.wake()
    return view


@router.patch("/streamers/{streamer_id}")
def update_streamer(
    streamer_id: int, body: StreamerPatch, ctx: AppContext = Depends(get_ctx)
) -> dict:
    with ctx.session_factory.begin() as db:
        streamer = _get_or_404(db, streamer_id)
        if body.enabled is not None and body.enabled != streamer.enabled:
            streamer.enabled = body.enabled
            write_audit(
                db,
                actor="user",
                action="streamer.enable" if body.enabled else "streamer.disable",
                object_id=f"streamer:{streamer.id}",
            )
            if not body.enabled:
                close_open_sessions(db, streamer.id, utcnow(), "disabled")
        if (
            body.permission_status is not None
            and body.permission_status != streamer.permission_status
        ):
            write_audit(
                db,
                actor="user",
                action="streamer.permission",
                object_id=f"streamer:{streamer.id}",
                details={"from": streamer.permission_status, "to": body.permission_status},
            )
            streamer.permission_status = body.permission_status
        if body.layout is not None:
            new_layout = (
                {"facecam": body.layout.facecam.model_dump()} if body.layout.facecam else None
            )
            if valid_facecam(new_layout) != valid_facecam(streamer.layout):
                streamer.layout = new_layout
                write_audit(
                    db,
                    actor="user",
                    action="streamer.layout",
                    object_id=f"streamer:{streamer.id}",
                    details={"facecam": valid_facecam(new_layout)},
                )
        db.flush()
        view = _view(streamer, _open_session(db, streamer.id), _status(ctx, streamer.id))
    if ctx.monitor:
        ctx.monitor.wake()
    return view


@router.get("/streamers/{streamer_id}/frame")
def latest_frame(streamer_id: int, ctx: AppContext = Depends(get_ctx)) -> Response:
    """A recent frame of this streamer's stream, for marking where the facecam is."""
    import subprocess

    with ctx.session_factory() as db:
        _get_or_404(db, streamer_id)
        session_ids = db.scalars(
            select(StreamSession.id)
            .where(StreamSession.streamer_id == streamer_id)
            .order_by(StreamSession.id.desc())
            .limit(3)
        ).all()
    if ctx.recorder is not None:
        for session_id in session_ids:
            segments = ctx.recorder.store.segments(session_id)
            if not segments:
                continue
            done = subprocess.run(
                [
                    ffmpeg_path(),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-i",
                    str(segments[-1].path),
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=960:-2",
                    "-q:v",
                    "4",
                    "-f",
                    "mjpeg",
                    "pipe:1",
                ],  # fmt: skip
                capture_output=True,
                timeout=30,
                **no_window_kwargs(),
            )
            if done.returncode == 0 and done.stdout:
                return Response(content=done.stdout, media_type="image/jpeg")
    raise HTTPException(
        status_code=404, detail="No recent footage yet. Try again while the streamer is live."
    )


@router.delete("/streamers/{streamer_id}")
def remove_streamer(streamer_id: int, ctx: AppContext = Depends(get_ctx)) -> dict:
    with ctx.session_factory.begin() as db:
        streamer = _get_or_404(db, streamer_id)
        close_open_sessions(db, streamer.id, utcnow(), "removed")
        write_audit(
            db,
            actor="user",
            action="streamer.remove",
            object_id=f"streamer:{streamer.id}",
            details={"platform": streamer.platform, "username": streamer.username},
        )
        db.delete(streamer)
    if ctx.monitor:
        ctx.monitor.forget(streamer_id)
    return {"id": streamer_id, "removed": True}
