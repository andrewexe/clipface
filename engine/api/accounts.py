"""Publishing accounts and post jobs."""

from __future__ import annotations

import logging
import secrets as pysecrets
import webbrowser
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from engine.api.app import AppContext, get_ctx
from engine.db.audit import write_audit
from engine.db.models import Account, Clip, PublishJob, utcnow
from engine.keychain import KeychainUnavailable
from engine.publish.errors import PublishError
from engine.publish.oauth import OAuthListener
from engine.publish.youtube import YouTubePublisher, authorization_url, new_pkce

log = logging.getLogger(__name__)
router = APIRouter()


class AccountPatch(BaseModel):
    daily_cap: int | None = Field(None, ge=0, le=50)
    paused: bool | None = None


class StrikeIn(BaseModel):
    kind: Literal["claim", "strike", "takedown"]
    note: str = Field("", max_length=300)


def _account_view(account: Account) -> dict:
    return {
        "id": account.id,
        "platform": account.platform,
        "handle": account.handle,
        "daily_cap": account.daily_cap,
        "paused": account.paused,
        "pause_reason": account.pause_reason,
    }


def _google_keys(ctx: AppContext) -> tuple[str, str] | None:
    try:
        client_id = ctx.secrets.get("google_client_id")
        client_secret = ctx.secrets.get("google_client_secret")
    except KeychainUnavailable:
        return None
    return (client_id, client_secret) if client_id and client_secret else None


def _youtube(ctx: AppContext) -> YouTubePublisher:
    platform = ctx.publisher.platforms.get("youtube") if ctx.publisher else None
    if not isinstance(platform, YouTubePublisher):
        raise HTTPException(status_code=503, detail="The publisher is not running.")
    return platform


@router.get("/accounts")
def list_accounts(ctx: AppContext = Depends(get_ctx)) -> dict:
    with ctx.session_factory() as db:
        accounts = [_account_view(a) for a in db.scalars(select(Account).order_by(Account.id))]
    flow = ctx.oauth_flow
    return {
        "accounts": accounts,
        "google_keys_set": _google_keys(ctx) is not None,
        "connecting": flow is not None and not flow.finished.is_set(),
        "connect_error": flow.error if flow is not None and flow.finished.is_set() else None,
        "privacy": ctx.settings.get().youtube_privacy,
    }


@router.post("/accounts/youtube/connect")
def connect_youtube(ctx: AppContext = Depends(get_ctx)) -> dict:
    """Start the Google sign-in in the user's browser."""
    youtube = _youtube(ctx)
    keys = _google_keys(ctx)
    if keys is None:
        raise HTTPException(
            status_code=409, detail="Add the Google OAuth client ID and secret in Settings first."
        )
    if ctx.oauth_flow is not None:
        ctx.oauth_flow.cancel()
    pkce = new_pkce()

    def finish(code: str, redirect_uri: str) -> str | None:
        try:
            tokens = youtube.client.exchange_code(code, redirect_uri, pkce)
            refresh = tokens["refresh_token"]
            channel = youtube.client.channel(refresh)
        except PublishError as exc:
            return str(exc)
        try:
            with ctx.session_factory.begin() as db:
                account = db.scalars(
                    select(Account).where(
                        Account.platform == "youtube", Account.handle == channel["title"]
                    )
                ).first()
                if account is None:
                    ref = f"acct_youtube_{pysecrets.token_hex(8)}"
                    account = Account(platform="youtube", handle=channel["title"], keychain_ref=ref)
                    db.add(account)
                    action = "account.connect"
                else:
                    action = "account.reconnect"
                    if account.pause_reason and account.pause_reason.startswith("Sign-in expired"):
                        account.paused, account.pause_reason = False, None
                # The refresh token goes to the OS keychain and nowhere else.
                ctx.secrets.set(account.keychain_ref, refresh)
                db.flush()
                write_audit(db, actor="user", action=action, object_id=f"account:{account.id}")
        except KeychainUnavailable as exc:
            return f"The sign-in worked, but the token could not be saved: {exc}"
        if ctx.publisher:
            ctx.publisher.wake()
        return None

    flow = OAuthListener(pkce.state, finish)
    flow.start()
    ctx.oauth_flow = flow
    url = authorization_url(keys[0], flow.redirect_uri, pkce)
    opened = False
    if ctx.open_browser:
        try:
            opened = bool(webbrowser.open(url))
        except Exception:  # noqa: BLE001 - no browser available: the UI shows the link
            opened = False
    return {"auth_url": url, "browser_opened": opened}


@router.patch("/accounts/{account_id}")
def update_account(account_id: int, body: AccountPatch, ctx: AppContext = Depends(get_ctx)) -> dict:
    with ctx.session_factory.begin() as db:
        account = db.get(Account, account_id)
        if account is None:
            raise HTTPException(status_code=404, detail="account not found")
        if body.daily_cap is not None and body.daily_cap != account.daily_cap:
            write_audit(
                db, actor="user", action="account.daily_cap", object_id=f"account:{account.id}",
                details={"from": account.daily_cap, "to": body.daily_cap},
            )  # fmt: skip
            account.daily_cap = body.daily_cap
        if body.paused is not None and body.paused != account.paused:
            account.paused = body.paused
            account.pause_reason = "Paused by you." if body.paused else None
            write_audit(
                db, actor="user", action="account.pause" if body.paused else "account.resume",
                object_id=f"account:{account.id}",
            )  # fmt: skip
        db.flush()
        view = _account_view(account)
    if ctx.publisher:
        ctx.publisher.wake()
    return view


@router.delete("/accounts/{account_id}")
def disconnect_account(account_id: int, ctx: AppContext = Depends(get_ctx)) -> dict:
    """Forget the account: cancel its waiting posts, delete its token, revoke the grant."""
    with ctx.session_factory.begin() as db:
        account = db.get(Account, account_id)
        if account is None:
            raise HTTPException(status_code=404, detail="account not found")
        ref, platform = account.keychain_ref, account.platform
        write_audit(
            db,
            actor="user",
            action="account.disconnect",
            object_id=f"account:{account.id}",
            details={"handle": account.handle},
        )
        db.delete(account)  # its jobs go with it
    if ref:
        try:
            token = ctx.secrets.get(ref)
            if token and platform == "youtube" and ctx.publisher:
                _youtube(ctx).client.revoke(token)
            ctx.secrets.delete(ref)
        except KeychainUnavailable:
            pass
    return {"id": account_id, "removed": True}


@router.post("/accounts/{account_id}/strike")
def report_strike(account_id: int, body: StrikeIn, ctx: AppContext = Depends(get_ctx)) -> dict:
    """Record a copyright claim, strike or takedown request: pauses the account."""
    if ctx.publisher is None:
        raise HTTPException(status_code=503, detail="The publisher is not running.")
    if not ctx.publisher.register_strike(account_id, body.kind, body.note.strip()):
        raise HTTPException(status_code=404, detail="account not found")
    with ctx.session_factory() as db:
        return _account_view(db.get(Account, account_id))


def _job_view(job: PublishJob, clip: Clip, account: Account, ctx: AppContext) -> dict:
    platform = ctx.publisher.platforms.get(account.platform) if ctx.publisher else None
    return {
        "id": job.id,
        "clip_id": clip.id,
        "title": clip.title,
        "account": account.handle,
        "platform": account.platform,
        "status": job.status,
        "scheduled_at": job.scheduled_at.isoformat(),
        "error": job.error,
        "url": platform.url(job.platform_post_id) if platform and job.platform_post_id else None,
        "removed": bool((job.state or {}).get("removed")),
    }


@router.get("/publish/jobs")
def list_jobs(limit: int = Query(50, ge=1, le=200), ctx: AppContext = Depends(get_ctx)) -> dict:
    with ctx.session_factory() as db:
        rows = db.execute(
            select(PublishJob, Clip, Account)
            .join(Clip, Clip.id == PublishJob.clip_id)
            .join(Account, Account.id == PublishJob.account_id)
            .order_by(PublishJob.scheduled_at.desc())
            .limit(limit)
        ).all()
        return {"jobs": [_job_view(job, clip, account, ctx) for job, clip, account in rows]}


def _change_job(ctx: AppContext, job_id: int, allowed: tuple[str, ...], new: str) -> dict:
    with ctx.session_factory.begin() as db:
        job = db.get(PublishJob, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="post not found")
        if job.status not in allowed:
            raise HTTPException(status_code=409, detail=f"A {job.status} post can't be changed.")
        job.status = new
        if new == "scheduled":
            job.scheduled_at, job.error = utcnow(), None
            job.state = {**(job.state or {}), "attempts": 0}
        action = "publish.retry" if new == "scheduled" else "publish.cancel"
        write_audit(db, actor="user", action=action, object_id=f"job:{job.id}")
        db.flush()
        view = _job_view(job, db.get(Clip, job.clip_id), db.get(Account, job.account_id), ctx)
    if ctx.publisher:
        ctx.publisher.wake()
    return view


@router.post("/publish/jobs/{job_id}/cancel")
def cancel_job(job_id: int, ctx: AppContext = Depends(get_ctx)) -> dict:
    return _change_job(ctx, job_id, ("scheduled", "failed"), "cancelled")


@router.post("/publish/jobs/{job_id}/retry")
def retry_job(job_id: int, ctx: AppContext = Depends(get_ctx)) -> dict:
    return _change_job(ctx, job_id, ("failed", "cancelled"), "scheduled")
