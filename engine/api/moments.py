"""Detected moments, newest first."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select

from engine.api.app import AppContext, get_ctx
from engine.db.models import Moment, Streamer, StreamSession

router = APIRouter()


@router.get("/moments")
def list_moments(limit: int = Query(50, ge=1, le=200), ctx: AppContext = Depends(get_ctx)) -> dict:
    with ctx.session_factory() as db:
        rows = db.execute(
            select(Moment, Streamer)
            .join(StreamSession, StreamSession.id == Moment.session_id)
            .join(Streamer, Streamer.id == StreamSession.streamer_id)
            .order_by(Moment.id.desc())
            .limit(limit)
        ).all()
    return {
        "signals": ctx.signals.status() if ctx.signals else None,
        "moments": [
            {
                "id": moment.id,
                "session_id": moment.session_id,
                "platform": streamer.platform,
                "username": streamer.username,
                "display_name": streamer.display_name,
                "peak_ts": moment.peak_ts.isoformat(),
                "status": moment.status,
                "signal_scores": moment.signal_scores,
            }
            for moment, streamer in rows
        ],
    }
