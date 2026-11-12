"""Safety controls: the kill switch and the autonomy level."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select

from engine.api.app import AppContext, get_ctx
from engine.db.audit import write_audit
from engine.db.models import AuditLog, utcnow

router = APIRouter(prefix="/control")

L3_WAIT = timedelta(days=14)


class KillSwitchIn(BaseModel):
    paused: bool


class AutonomyIn(BaseModel):
    level: Literal["L1", "L2", "L3"]


def _l3_status(ctx: AppContext) -> dict:
    """L3 needs two weeks of L2 with no strikes. This only reports whether that holds."""
    settings = ctx.settings.get()
    if settings.autonomy_level != "L2" or not settings.autonomy_l2_since:
        return {"eligible": False, "reason": "L2 has to run for two weeks with no strikes first."}
    since = datetime.fromisoformat(settings.autonomy_l2_since)
    with ctx.session_factory() as db:
        strikes = db.scalar(
            select(func.count()).where(AuditLog.action == "account.strike", AuditLog.ts >= since)
        )
    if strikes:
        return {
            "eligible": False,
            "reason": f"{strikes} strike(s) since L2 was switched on. The two weeks start again "
            "when you switch L2 off and on.",
        }
    remaining = since + L3_WAIT - datetime.now(UTC)
    if remaining > timedelta(0):
        return {
            "eligible": False,
            "reason": f"L2 needs {remaining.days + 1} more day(s) without a strike.",
        }
    return {"eligible": True, "reason": "Two weeks of L2 with no strikes."}


def _view(ctx: AppContext) -> dict:
    settings = ctx.settings.get()
    return {
        "publishing_paused": settings.publishing_paused,
        "autonomy_level": settings.autonomy_level,
        "autonomy_l2_since": settings.autonomy_l2_since or None,
        "l3": _l3_status(ctx),
    }


@router.get("")
def get_control(ctx: AppContext = Depends(get_ctx)) -> dict:
    return _view(ctx)


@router.post("/kill-switch")
def kill_switch(body: KillSwitchIn, ctx: AppContext = Depends(get_ctx)) -> dict:
    """Pause (or resume) all publishing at once."""
    _settings, changes = ctx.settings.update({"publishing_paused": body.paused})
    if changes:
        with ctx.session_factory.begin() as db:
            write_audit(
                db,
                actor="user",
                action="publishing.pause" if body.paused else "publishing.resume",
            )
        for service in ctx.services():
            if service is not None:
                service.wake()
    return _view(ctx)


@router.post("/autonomy")
def set_autonomy(body: AutonomyIn, ctx: AppContext = Depends(get_ctx)) -> dict:
    if body.level == "L3":
        status = _l3_status(ctx)
        detail = (
            "L3 is not available. The plan says when it unlocks but not what it changes, "
            "so it is not built."
            if status["eligible"]
            else f"L3 is not available yet. {status['reason']}"
        )
        raise HTTPException(status_code=409, detail=detail)
    patch = {"autonomy_level": body.level}
    if body.level == "L2" and ctx.settings.get().autonomy_level != "L2":
        patch["autonomy_l2_since"] = utcnow().isoformat()
    if body.level == "L1":
        patch["autonomy_l2_since"] = ""
    _settings, changes = ctx.settings.update(patch)
    if "autonomy_level" in changes:
        with ctx.session_factory.begin() as db:
            write_audit(db, actor="user", action="autonomy.set", details=changes["autonomy_level"])
    return _view(ctx)
