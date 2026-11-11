"""Live monitor: polls each platform and turns "is live" into rows in `sessions`.

A streamer is live exactly when they have a session with no `ended_at`. Going live opens
a session; going offline closes it. Both are written to the audit log.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from engine.db.audit import write_audit
from engine.db.models import Streamer, StreamSession, utcnow
from engine.monitor.platforms import (
    LiveInfo,
    MissingCredentials,
    PlatformAuthError,
    PlatformError,
    Snapshot,
)

log = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL_S = 20.0
# A live stream can vanish from one API response. Wait for this many misses in a row.
DEFAULT_OFFLINE_GRACE_POLLS = 2


class PlatformClient(Protocol):
    async def check(self, usernames: Sequence[str], verify: Collection[str]) -> Snapshot: ...
    async def aclose(self) -> None: ...


@dataclass
class PlatformStatus:
    state: str = "idle"  # idle | ok | needs_keys | bad_keys | error
    message: str | None = None
    last_ok_at: datetime | None = None
    retry_at: float = 0.0  # monotonic; skip polling until then

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "message": self.message,
            "last_ok_at": self.last_ok_at.isoformat() if self.last_ok_at else None,
        }


@dataclass
class StreamerStatus:
    exists: bool | None = None  # None until the platform has confirmed either way
    last_checked_at: datetime | None = None
    misses: int = 0
    viewer_count: int | None = None


@dataclass(frozen=True)
class _Row:
    id: int
    platform: str
    username: str


SessionListener = Callable[[str, int], None]  # ("start" | "end", session id)


def close_open_sessions(db: Session, streamer_id: int, when: datetime, reason: str) -> list[int]:
    """End every open session for a streamer. Returns the ids that were closed."""
    closed = []
    open_sessions = db.scalars(
        select(StreamSession).where(
            StreamSession.streamer_id == streamer_id, StreamSession.ended_at.is_(None)
        )
    ).all()
    for stream in open_sessions:
        stream.ended_at = max(when, stream.started_at)
        write_audit(
            db,
            actor="system",
            action="session.end",
            object_id=f"session:{stream.id}",
            details={"streamer_id": streamer_id, "reason": reason},
        )
        closed.append(stream.id)
    return closed


class Monitor:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        clients: dict[str, PlatformClient],
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        offline_grace_polls: int = DEFAULT_OFFLINE_GRACE_POLLS,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self._session_factory = session_factory
        self._clients = clients
        self.poll_interval_s = poll_interval_s
        self._grace = max(1, offline_grace_polls)
        self._now = now
        self._platforms = {name: PlatformStatus() for name in clients}
        self._streamers: dict[int, StreamerStatus] = {}
        self._listeners: list[SessionListener] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self.running = False

    # -- public surface -------------------------------------------------------------

    def add_listener(self, listener: SessionListener) -> None:
        self._listeners.append(listener)

    def wake(self) -> None:
        """Poll now instead of waiting out the interval. Safe to call from any thread."""
        if self._loop is not None and self._wake is not None:
            self._loop.call_soon_threadsafe(self._wake.set)

    def platform_status(self) -> dict[str, dict]:
        return {name: status.as_dict() for name, status in self._platforms.items()}

    def streamer_status(self, streamer_id: int) -> StreamerStatus:
        return self._streamers.get(streamer_id, StreamerStatus())

    def forget(self, streamer_id: int) -> None:
        self._streamers.pop(streamer_id, None)

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        self.running = True
        log.info("live monitor started (every %.0fs)", self.poll_interval_s)
        try:
            while True:
                self._wake.clear()
                try:
                    await self.poll_once()
                except Exception:
                    log.exception("live monitor poll failed")
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=self.poll_interval_s)
                except TimeoutError:
                    pass
        finally:
            self.running = False

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()

    # -- one poll -------------------------------------------------------------------

    async def poll_once(self) -> None:
        rows = await asyncio.to_thread(self._load)
        by_platform: dict[str, list[_Row]] = {name: [] for name in self._clients}
        for row in rows:
            if row.platform in by_platform:
                by_platform[row.platform].append(row)
        snapshots = await asyncio.gather(
            *(self._check(platform, group) for platform, group in by_platform.items())
        )
        events: list[tuple[str, int]] = []
        for (_platform, group), snapshot in zip(by_platform.items(), snapshots, strict=True):
            if snapshot is not None:
                events += await asyncio.to_thread(self._apply, group, snapshot)
        for kind, session_id in events:
            for listener in self._listeners:
                try:
                    listener(kind, session_id)
                except Exception:
                    log.exception("session listener failed")

    def _load(self) -> list[_Row]:
        """Enabled streamers. Also closes sessions left open for disabled ones."""
        with self._session_factory.begin() as db:
            streamers = db.scalars(select(Streamer)).all()
            rows = []
            for streamer in streamers:
                if streamer.enabled:
                    rows.append(_Row(streamer.id, streamer.platform, streamer.username))
                else:
                    close_open_sessions(db, streamer.id, self._now(), "disabled")
            return rows

    async def _check(self, platform: str, rows: list[_Row]) -> Snapshot | None:
        status = self._platforms[platform]
        if not rows:
            if status.state == "ok":
                status.state = "idle"
            return None
        if time.monotonic() < status.retry_at:
            return None
        usernames = [row.username for row in rows]
        verify = {row.username for row in rows if self.streamer_status(row.id).exists is None}
        try:
            snapshot = await self._clients[platform].check(usernames, verify)
        except MissingCredentials:
            status.state, status.message = "needs_keys", "Add this platform's keys in Settings."
            return None
        except PlatformAuthError as exc:
            status.state, status.message = "bad_keys", str(exc)
            return None
        except PlatformError as exc:
            status.state, status.message = "error", str(exc)
            status.retry_at = time.monotonic() + (exc.retry_after_s or 0.0)
            log.warning("%s check failed: %s", platform, exc)
            return None
        except Exception:
            status.state, status.message = "error", "Unexpected error; see the engine log."
            log.exception("%s check crashed", platform)
            return None
        status.state, status.message, status.last_ok_at = "ok", None, self._now()
        return snapshot

    def _apply(self, rows: list[_Row], snapshot: Snapshot) -> list[tuple[str, int]]:
        now = self._now()
        events: list[tuple[str, int]] = []
        with self._session_factory.begin() as db:
            for row in rows:
                streamer = db.get(Streamer, row.id)
                if streamer is None or not streamer.enabled:
                    continue  # removed or disabled while the request was in flight
                state = self._streamers.setdefault(row.id, StreamerStatus())
                state.last_checked_at = now
                info = snapshot.live.get(row.username)
                if row.username in snapshot.found:
                    state.exists = True
                    if streamer.display_name != snapshot.found[row.username]:
                        streamer.display_name = snapshot.found[row.username]
                elif row.username in snapshot.missing:
                    state.exists = False
                if info is not None:
                    state.exists = True
                state.viewer_count = info.viewer_count if info else None
                events += self._reconcile(db, streamer, state, info, now)
        return events

    def _reconcile(
        self,
        db: Session,
        streamer: Streamer,
        state: StreamerStatus,
        info: LiveInfo | None,
        now: datetime,
    ) -> list[tuple[str, int]]:
        current = db.scalars(
            select(StreamSession)
            .where(StreamSession.streamer_id == streamer.id, StreamSession.ended_at.is_(None))
            .order_by(StreamSession.started_at.desc())
        ).first()

        if info is None:
            if current is None:
                state.misses = 0
                return []
            state.misses += 1
            if state.misses < self._grace:
                return []
            state.misses = 0
            return [("end", sid) for sid in close_open_sessions(db, streamer.id, now, "offline")]

        state.misses = 0
        events: list[tuple[str, int]] = []
        if current is not None:
            same_stream = info.started_at is None or info.started_at == current.started_at
            if same_stream:
                current.title, current.game = info.title, info.game
                return []
            # A new broadcast began before we saw the old one end.
            ended = close_open_sessions(db, streamer.id, info.started_at or now, "new_stream")
            events += [("end", sid) for sid in ended]

        stream = StreamSession(
            streamer_id=streamer.id,
            started_at=info.started_at or now,
            title=info.title,
            game=info.game,
        )
        db.add(stream)
        db.flush()
        write_audit(
            db,
            actor="system",
            action="session.start",
            object_id=f"session:{stream.id}",
            details={
                "streamer_id": streamer.id,
                "platform": streamer.platform,
                "username": streamer.username,
            },
        )
        log.info("%s/%s went live", streamer.platform, streamer.username)
        events.append(("start", stream.id))
        return events
