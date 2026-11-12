"""Publisher: schedules approved clips onto connected accounts and posts them.

Rules it enforces:
- One job per clip per account, guaranteed by a unique idempotency key.
- Posts are spaced by a random gap, stay inside a daily time window, and respect each
  account's daily cap.
- Every condition is checked again at the moment of posting: the kill switch, the
  account, the streamer's permission, the clip's status and its policy verdict.
- A claim, strike or takedown pauses the account and raises an alert.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from engine.control.autonomy import demote_to_l2
from engine.db.audit import write_audit
from engine.db.models import (
    Account,
    Clip,
    Metric,
    Moment,
    PublishJob,
    Streamer,
    StreamSession,
    utcnow,
)
from engine.keychain import KeychainUnavailable, SecretStore
from engine.monitor.urls import StreamerRef
from engine.policy.gate import PERMITTED
from engine.policy.rules import build_description, normalise_rules
from engine.publish.errors import (
    PublishAuthError,
    PublishError,
    PublishPending,
    PublishRejected,
)
from engine.settings import AppSettings, SettingsStore

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
MAX_WAITS = 60  # two hours of two-minute checks
ACTIVE = ("scheduled", "running", "succeeded")
STRIKE_REASONS = {"copyright", "claim", "trademark", "legal", "termsOfUse", "inappropriate"}
POST_CHECK_INTERVAL_S = 1800
POST_CHECK_DAYS = 14


class PlatformPublisher(Protocol):
    platform: str

    def publish(
        self,
        token: str,
        state: dict,
        save_state: Callable[[dict], None],
        video: Path,
        title: str,
        description: str,
        tags: list[str],
        privacy: str,
    ) -> str: ...

    def remove(self, token: str, post_id: str) -> bool: ...

    def states(self, token: str, post_ids: list[str]) -> dict[str, dict]: ...

    def url(self, post_id: str) -> str: ...


def idempotency_key(clip_id: int, account_id: int) -> str:
    return hashlib.sha256(f"clip:{clip_id}:account:{account_id}".encode()).hexdigest()[:32]


class Publisher:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: SettingsStore,
        secrets: SecretStore,
        platforms: dict[str, PlatformPublisher],
        now: Callable[[], datetime] = utcnow,
        uniform: Callable[[float, float], float] = random.uniform,
        tz: tzinfo | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._secrets = secrets
        self.platforms = platforms
        self._now = now
        self._uniform = uniform
        self._tz = tz  # None: the computer's local time zone
        self._alert_listeners: list[Callable[[str], None]] = []
        self._run_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self._last_post_check = 0.0

    # -- wiring ---------------------------------------------------------------------

    def on_alert(self, listener: Callable[[str], None]) -> None:
        self._alert_listeners.append(listener)

    def _alert(self, text: str) -> None:
        log.warning("ALERT: %s", text)
        for listener in self._alert_listeners:
            try:
                listener(text)
            except Exception:
                log.exception("alert listener failed")

    def wake(self) -> None:
        if self._loop is not None and self._wake is not None:
            self._loop.call_soon_threadsafe(self._wake.set)

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        await asyncio.to_thread(self.recover)
        log.info("publisher started")
        while True:
            self._wake.clear()
            try:
                await asyncio.to_thread(self.run_due)
                if self._loop.time() - self._last_post_check > POST_CHECK_INTERVAL_S:
                    self._last_post_check = self._loop.time()
                    await asyncio.to_thread(self.check_posts)
            except Exception:
                log.exception("publisher pass failed")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=15.0)
            except TimeoutError:
                pass

    async def aclose(self) -> None:
        pass

    # -- scheduling -----------------------------------------------------------------

    def schedule_clip(self, clip_id: int) -> list[int]:
        """Create a post job for an approved clip on every usable account. Calling it
        again for the same clip creates nothing new. Returns the new job ids."""
        settings = self._settings.get()
        created: list[int] = []
        with self._session_factory() as db:
            clip = db.get(Clip, clip_id)
            if clip is None or clip.status != "approved":
                return []
            streamer = self._streamer_of(db, clip)
            if streamer.permission_status not in PERMITTED:
                return []  # no permission: export only, never posted by the app
            account_ids = db.scalars(
                select(Account.id).where(Account.platform.in_(list(self.platforms)))
            ).all()
        for account_id in account_ids:
            try:
                with self._session_factory.begin() as db:
                    account = db.get(Account, account_id)
                    job = PublishJob(
                        clip_id=clip_id,
                        account_id=account_id,
                        scheduled_at=self._next_slot(db, account, settings),
                        idempotency_key=idempotency_key(clip_id, account_id),
                        status="scheduled",
                        state={"attempts": 0},
                    )
                    db.add(job)
                    db.flush()
                    write_audit(
                        db,
                        actor="system",
                        action="publish.schedule",
                        object_id=f"job:{job.id}",
                        details={
                            "clip_id": clip_id,
                            "account_id": account_id,
                            "at": job.scheduled_at.isoformat(),
                        },
                    )
                    created.append(job.id)
            except IntegrityError:
                continue  # this clip is already scheduled on this account
        if created:
            self.wake()
        return created

    def _local(self, moment: datetime) -> datetime:
        return moment.astimezone(self._tz)

    def _next_slot(self, db: Session, account: Account, settings: AppSettings) -> datetime:
        now = self._now()
        last = db.scalar(
            select(PublishJob.scheduled_at)
            .where(PublishJob.account_id == account.id, PublishJob.status.in_(ACTIVE))
            .order_by(PublishJob.scheduled_at.desc())
            .limit(1)
        )
        low = min(settings.post_gap_min_minutes, settings.post_gap_max_minutes)
        high = max(settings.post_gap_min_minutes, settings.post_gap_max_minutes)
        if last is not None and last + timedelta(minutes=low) > now:
            slot = last + timedelta(minutes=self._uniform(low, high))
        else:
            slot = now + timedelta(minutes=self._uniform(1, 10))  # a short window to cancel

        start_hour, end_hour = settings.post_window_start_hour, settings.post_window_end_hour
        if end_hour <= start_hour:
            start_hour, end_hour = 0, 24
        for _ in range(90):
            local = self._local(slot)
            if local.hour < start_hour:
                slot = self._window_start(local, start_hour)
            elif local.hour >= end_hour:
                slot = self._window_start(local + timedelta(days=1), start_hour)
            elif self._posts_on_day(db, account.id, local) >= max(0, account.daily_cap):
                slot = self._window_start(local + timedelta(days=1), start_hour)
            else:
                return slot
        return slot

    def _window_start(self, local_day: datetime, start_hour: int) -> datetime:
        start = local_day.replace(hour=start_hour, minute=0, second=0, microsecond=0)
        return start + timedelta(minutes=self._uniform(0, 45))

    def _posts_on_day(self, db: Session, account_id: int, local_day: datetime) -> int:
        day_start = local_day.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        return len(
            db.scalars(
                select(PublishJob.id).where(
                    PublishJob.account_id == account_id,
                    PublishJob.status.in_(ACTIVE),
                    PublishJob.scheduled_at >= day_start,
                    PublishJob.scheduled_at < day_end,
                )
            ).all()
        )

    # -- posting --------------------------------------------------------------------

    def recover(self) -> None:
        """Jobs left "running" by a crash go back to "scheduled". Their saved upload
        session makes the retry continue instead of starting a second upload."""
        with self._session_factory.begin() as db:
            for job in db.scalars(select(PublishJob).where(PublishJob.status == "running")):
                job.status = "scheduled"

    def run_due(self) -> None:
        if self._settings.get().publishing_paused:
            return
        with self._session_factory() as db:
            due = db.scalars(
                select(PublishJob.id)
                .join(Account, Account.id == PublishJob.account_id)
                .where(
                    PublishJob.status == "scheduled",
                    PublishJob.scheduled_at <= self._now(),
                    Account.paused.is_(False),
                )
                .order_by(PublishJob.scheduled_at)
            ).all()
        for job_id in due:
            self.run_job(job_id)

    def _streamer_of(self, db: Session, clip: Clip) -> Streamer:
        moment = db.get(Moment, clip.moment_id)
        return db.get(Streamer, db.get(StreamSession, moment.session_id).streamer_id)

    def _blockers(
        self, settings: AppSettings, clip: Clip | None, streamer: Streamer | None, account: Account
    ) -> list[str]:
        """Reasons not to post right now. Checked at the last moment, every time."""
        reasons = []
        if clip is None or clip.status not in ("approved", "published"):
            reasons.append("the clip is no longer approved")
        elif (clip.policy_result or {}).get("verdict") == "block" and not (clip.meta or {}).get(
            "reviewed_at"
        ):
            reasons.append("the clip was rejected by the safety checks")
        if streamer is None or streamer.permission_status not in PERMITTED:
            reasons.append("the streamer's permission was withdrawn")
        elif not streamer.enabled:
            reasons.append("the streamer is paused")
        if clip is not None and not Path(clip.file_path or "").is_file():
            reasons.append("the clip's video file is missing")
        if account.platform not in self.platforms:
            reasons.append(f"posting to {account.platform} is not set up")
        return reasons

    def run_job(self, job_id: int) -> str:
        """Post one job. Returns its status afterwards. Only one run at a time, and a job
        is claimed inside a transaction, so the same job can never be posted twice."""
        with self._run_lock:
            return self._run_job(job_id)

    def _run_job(self, job_id: int) -> str:
        settings = self._settings.get()
        with self._session_factory.begin() as db:
            job = db.get(PublishJob, job_id)
            if job is None or job.status != "scheduled":
                return job.status if job else "missing"
            account = db.get(Account, job.account_id)
            if settings.publishing_paused or account.paused:
                return "scheduled"
            clip = db.get(Clip, job.clip_id)
            streamer = self._streamer_of(db, clip) if clip else None
            blockers = self._blockers(settings, clip, streamer, account)
            if blockers:
                job.status, job.error = "cancelled", "; ".join(blockers)
                write_audit(
                    db, actor="system", action="publish.cancel", object_id=f"job:{job.id}",
                    details={"why": blockers},
                )  # fmt: skip
                return "cancelled"
            local = self._local(self._now())
            done_today = len(
                db.scalars(
                    select(PublishJob.id).where(
                        PublishJob.account_id == account.id,
                        PublishJob.status == "succeeded",
                        PublishJob.scheduled_at
                        >= local.replace(hour=0, minute=0, second=0, microsecond=0),
                    )
                ).all()
            )
            if done_today >= account.daily_cap:
                tomorrow = self._window_start(
                    local + timedelta(days=1), settings.post_window_start_hour
                )
                job.scheduled_at = tomorrow
                return "scheduled"
            job.status = "running"
            state = dict(job.state or {})
            state["attempts"] = int(state.get("attempts", 0)) + 1
            job.state = state
            platform = self.platforms[account.platform]
            name = streamer.display_name or streamer.username
            url = StreamerRef(streamer.platform, streamer.username).url
            rules = normalise_rules(streamer.program_rules)
            args = {
                "video": Path(clip.file_path),
                "title": clip.title or f"{name} clip",
                "description": build_description(clip.caption, name, url, rules),
                "tags": rules["required_tags"],
                "privacy": settings.youtube_privacy,
            }
            token_ref, account_id, clip_id = account.keychain_ref, account.id, clip.id

        def save_state(extra: dict) -> None:
            with self._session_factory.begin() as db:
                row = db.get(PublishJob, job_id)
                row.state = {**(row.state or {}), **extra}
            state.update(extra)

        try:
            token = self._secrets.get(token_ref) if token_ref else None
            if not token:
                raise PublishAuthError("this account has no saved sign-in")
            post_id = platform.publish(token, state, save_state, **args)
        except (PublishAuthError, KeychainUnavailable) as exc:
            return self._auth_failed(job_id, account_id, str(exc))
        except PublishRejected as exc:
            return self._finish(job_id, "failed", error=str(exc))
        except PublishPending as exc:
            return self._wait_for_platform(job_id, str(exc))
        except PublishError as exc:
            return self._retry_later(job_id, str(exc), state["attempts"])
        except Exception as exc:  # noqa: BLE001 - never let one job stop the publisher
            log.exception("publish job %s crashed", job_id)
            return self._retry_later(
                job_id, f"unexpected error ({type(exc).__name__})", state["attempts"]
            )

        with self._session_factory.begin() as db:
            job = db.get(PublishJob, job_id)
            job.status, job.platform_post_id, job.error = "succeeded", post_id, None
            clip = db.get(Clip, clip_id)
            if clip.status == "approved":
                clip.status = "published"
            write_audit(
                db,
                actor="system",
                action="clip.publish",
                object_id=f"clip:{clip_id}",
                details={"job_id": job_id, "account_id": account_id, "post_id": post_id},
            )
        log.info("clip %s posted to account %s", clip_id, account_id)
        return "succeeded"

    def _finish(self, job_id: int, status: str, error: str) -> str:
        with self._session_factory.begin() as db:
            job = db.get(PublishJob, job_id)
            job.status, job.error = status, error
            write_audit(
                db, actor="system", action="publish.fail", object_id=f"job:{job_id}",
                details={"error": error},
            )  # fmt: skip
        return status

    def _retry_later(self, job_id: int, error: str, attempts: int) -> str:
        if attempts >= MAX_ATTEMPTS:
            return self._finish(job_id, "failed", f"{error} (gave up after {attempts} tries)")
        with self._session_factory.begin() as db:
            job = db.get(PublishJob, job_id)
            job.status, job.error = "scheduled", error
            job.scheduled_at = self._now() + timedelta(minutes=2 ** min(attempts, 6))
        return "scheduled"

    def _wait_for_platform(self, job_id: int, note: str) -> str:
        """The platform is still processing: check again in two minutes. This is not a
        failure, so it does not use up the job's attempts, but it does not wait forever."""
        with self._session_factory.begin() as db:
            job = db.get(PublishJob, job_id)
            state = dict(job.state or {})
            state["waits"] = int(state.get("waits", 0)) + 1
            state["attempts"] = max(0, int(state.get("attempts", 1)) - 1)
            job.state = state
            if state["waits"] > MAX_WAITS:
                job.status, job.error = "failed", "the platform never finished processing it"
                return "failed"
            job.status, job.error = "scheduled", note
            job.scheduled_at = self._now() + timedelta(minutes=2)
        return "scheduled"

    def rotate_token(self, platform: str, old: str, new: str) -> None:
        """A platform issued a new refresh token: store it in place of the old one."""
        with self._session_factory() as db:
            refs = db.scalars(
                select(Account.keychain_ref).where(Account.platform == platform)
            ).all()
        for ref in refs:
            try:
                if ref and self._secrets.get(ref) == old:
                    self._secrets.set(ref, new)
            except KeychainUnavailable:
                log.warning("could not store a rotated %s token", platform)

    def _auth_failed(self, job_id: int, account_id: int, error: str) -> str:
        with self._session_factory.begin() as db:
            job = db.get(PublishJob, job_id)
            job.status, job.error = "scheduled", error
            account = db.get(Account, account_id)
            account.paused = True
            account.pause_reason = "Sign-in expired. Disconnect and connect this account again."
            write_audit(
                db,
                actor="system",
                action="account.signin_expired",
                object_id=f"account:{account_id}",
            )
            handle = account.handle
        self._alert(f"{handle}: sign-in expired, posting is paused until you reconnect it.")
        return "scheduled"

    # -- strikes, removal, metrics ----------------------------------------------------

    def register_strike(self, account_id: int, kind: str, note: str = "") -> bool:
        """A claim, strike or takedown: pause the account and raise an alert."""
        with self._session_factory.begin() as db:
            account = db.get(Account, account_id)
            if account is None:
                return False
            account.paused = True
            account.pause_reason = f"{kind}: {note}".strip(": ") or kind
            write_audit(
                db,
                actor="system",
                action="account.strike",
                object_id=f"account:{account_id}",
                details={"kind": kind, "note": note},
            )
            handle = account.handle
        dropped = demote_to_l2(self._settings, self._session_factory, f"{kind} on {handle}")
        self._alert(
            f"{handle} is paused: {kind}. {note}".strip()
            + (" Autonomy dropped from L3 to L2." if dropped else "")
        )
        return True

    def remove_clip(self, clip_id: int) -> bool:
        """Take a clip's posts down from every platform. True if all of them are gone."""
        with self._session_factory() as db:
            jobs = db.execute(
                select(
                    PublishJob.id,
                    PublishJob.platform_post_id,
                    Account.platform,
                    Account.keychain_ref,
                )
                .join(Account, Account.id == PublishJob.account_id)
                .where(PublishJob.clip_id == clip_id, PublishJob.status == "succeeded")
            ).all()
        all_removed = True
        for job_id, post_id, platform_name, token_ref in jobs:
            removed = False
            try:
                platform = self.platforms.get(platform_name)
                token = self._secrets.get(token_ref) if token_ref else None
                removed = bool(platform and token and post_id and platform.remove(token, post_id))
            except (PublishError, KeychainUnavailable) as exc:
                log.warning("could not remove post of job %s: %s", job_id, exc)
            if removed:
                with self._session_factory.begin() as db:
                    job = db.get(PublishJob, job_id)
                    job.state = {**(job.state or {}), "removed": True}
            all_removed = all_removed and removed
        return all_removed

    def check_posts(self) -> None:
        """Look at recent posts: record their numbers, and treat a post the platform
        rejected or removed as a strike."""
        since = self._now() - timedelta(days=POST_CHECK_DAYS)
        with self._session_factory() as db:
            rows = db.execute(
                select(PublishJob.id, PublishJob.platform_post_id, PublishJob.state, Account.id,
                       Account.platform, Account.keychain_ref)
                .join(Account, Account.id == PublishJob.account_id)
                .where(PublishJob.status == "succeeded", PublishJob.scheduled_at >= since)
            ).all()  # fmt: skip
        by_account: dict[tuple[int, str, str | None], list[tuple[int, str, dict]]] = {}
        for job_id, post_id, state, account_id, platform, ref in rows:
            if post_id and not (state or {}).get("removed") and not (state or {}).get("strike"):
                by_account.setdefault((account_id, platform, ref), []).append(
                    (job_id, post_id, state or {})
                )
        for (account_id, platform_name, ref), posts in by_account.items():
            platform = self.platforms.get(platform_name)
            try:
                token = self._secrets.get(ref) if ref else None
                if not platform or not token:
                    continue
                states = platform.states(token, [post_id for _j, post_id, _s in posts])
            except (PublishError, KeychainUnavailable) as exc:
                log.info("could not check posts of account %s: %s", account_id, exc)
                continue
            for job_id, post_id, _state in posts:
                seen = states.get(post_id)
                problem = None
                if seen is None:
                    problem = ("removed", "A post is no longer on the platform.")
                elif seen.get("upload_status") == "rejected":
                    reason = seen.get("rejection_reason") or "rejected"
                    if reason in STRIKE_REASONS:
                        problem = (reason, "The platform rejected a post.")
                with self._session_factory.begin() as db:
                    if seen is not None:
                        db.add(
                            Metric(job_id=job_id, post_id=post_id, checked_at=self._now(),
                                   views=seen.get("views", 0), likes=seen.get("likes", 0),
                                   comments=seen.get("comments", 0))
                        )  # fmt: skip
                    if problem:
                        job = db.get(PublishJob, job_id)
                        job.state = {**(job.state or {}), "strike": problem[0]}
                if problem:
                    self.register_strike(
                        account_id, problem[0], f"{problem[1]} ({platform.url(post_id)})"
                    )
