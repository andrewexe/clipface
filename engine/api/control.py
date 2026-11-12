"""Safety controls: the kill switch and the autonomy level."""

from __future__ import annotations

import secrets as pysecrets
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select

from engine.api.app import AppContext, get_ctx
from engine.control.bot_actions import set_publishing_paused
from engine.db.audit import write_audit
from engine.db.models import AuditLog, utcnow
from engine.keychain import KeychainUnavailable

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
    set_publishing_paused(ctx, body.paused)
    return _view(ctx)


def _telegram_view(ctx: AppContext) -> dict:
    bot = ctx.telegram
    settings = ctx.settings.get()
    paired = bool(settings.telegram_chat_id)
    return {
        "state": bot.state if bot else "off",
        "bot_username": bot.bot_username if bot else None,
        "paired": paired,
        # Shown in the app only; whoever can read it here already controls the app.
        "pairing_code": None if paired or bot is None else bot.pairing_code,
        "notify": settings.telegram_notify,
    }


@router.get("/telegram")
def telegram_status(ctx: AppContext = Depends(get_ctx)) -> dict:
    return _telegram_view(ctx)


@router.post("/telegram/unpair")
def telegram_unpair(ctx: AppContext = Depends(get_ctx)) -> dict:
    _settings, changes = ctx.settings.update({"telegram_chat_id": 0})
    if changes:
        with ctx.session_factory.begin() as db:
            write_audit(db, actor="user", action="telegram.unpair")
    return _telegram_view(ctx)


@router.post("/telegram/test")
def telegram_test(ctx: AppContext = Depends(get_ctx)) -> dict:
    """Send a test message to the paired phone."""
    if ctx.telegram is None or not ctx.settings.get().telegram_chat_id:
        raise HTTPException(status_code=409, detail="No phone is paired yet.")
    ctx.telegram.alert("Test message from Clipper. Phone approvals are working.")
    return _telegram_view(ctx)


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


class McpIn(BaseModel):
    enabled: bool | None = None
    can_approve: bool | None = None


def _mcp_view(ctx: AppContext) -> dict:
    settings = ctx.settings.get()
    return {"enabled": settings.mcp_enabled, "can_approve": settings.mcp_can_approve}


@router.get("/mcp")
def mcp_status(ctx: AppContext = Depends(get_ctx)) -> dict:
    return _mcp_view(ctx)


@router.post("/mcp")
def mcp_set(body: McpIn, ctx: AppContext = Depends(get_ctx)) -> dict:
    """Turn the MCP bridge on or off, and decide whether it may approve clips."""
    patch = {}
    if body.enabled is not None:
        patch["mcp_enabled"] = body.enabled
    if body.can_approve is not None:
        patch["mcp_can_approve"] = body.can_approve
    try:
        if body.enabled is True and not ctx.settings.get().mcp_enabled:
            # A fresh token each time it is switched on; kept in the OS keychain only.
            ctx.secrets.set("mcp_bridge_token", pysecrets.token_urlsafe(32))
        elif body.enabled is False:
            ctx.secrets.delete("mcp_bridge_token")
    except KeychainUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    _settings, changes = ctx.settings.update(patch)
    if changes:
        with ctx.session_factory.begin() as db:
            write_audit(db, actor="user", action="mcp.settings", details=changes)
    return _mcp_view(ctx)
