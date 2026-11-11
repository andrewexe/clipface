"""Signal service: feeds live signals to one detector per live session and saves the
moments it finds.

Signals: Twitch chat rate and hype-message rate (anonymous IRC), audio loudness of the
recorded segments (both platforms), plus viewer count and viewer-made clip counts as
context. Kick has no chat signal: its public API cannot read chat.
"""

from __future__ import annotations

import asyncio
import logging
import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from engine.db.audit import write_audit
from engine.db.models import Moment, Streamer, StreamSession
from engine.detector.core import Candidate, DetectorConfig, MomentDetector, SignalConfig
from engine.monitor.platforms import PlatformError
from engine.monitor.service import Monitor
from engine.recorder.store import SegmentStore
from engine.settings import AppSettings, SettingsStore
from engine.signals.audio import loudness_per_second
from engine.signals.hype import is_hype
from engine.signals.twitch_chat import ChatMessage, TwitchChat

log = logging.getLogger(__name__)

CLIP_WINDOW_S = 90
_CLIP_POLL_S = 30
_VIEWER_HISTORY_S = 300


class ClipsClient(Protocol):
    async def recent_clips(self, login: str, since: datetime) -> list[datetime]: ...


@dataclass
class _Tracked:
    session_id: int
    streamer_id: int
    platform: str
    username: str
    detector: MomentDetector
    audio_done: set[str] = field(default_factory=set)
    viewers: list[tuple[float, int]] = field(default_factory=list)
    clip_times: list[float] = field(default_factory=list)


def detector_config(settings: AppSettings, platform: str) -> DetectorConfig:
    signals: dict[str, SignalConfig] = {}
    if platform == "twitch":
        signals["chat"] = SignalConfig(threshold=settings.detect_z_threshold, min_events=8)
        signals["hype"] = SignalConfig(threshold=settings.detect_z_threshold, min_events=6)
    if settings.detect_audio:
        signals["audio"] = SignalConfig(
            threshold=settings.detect_audio_z, kind="level", min_scale=1.5
        )
    return DetectorConfig(cooldown_s=settings.detect_cooldown_s, signals=signals)


class SignalService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: SettingsStore,
        store: SegmentStore,
        monitor: Monitor | None = None,
        clips: ClipsClient | None = None,
        chat_factory: Callable[[Callable[[ChatMessage], None]], TwitchChat] = TwitchChat,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._store = store
        self._monitor = monitor
        self._clips = clips
        self._clock = clock
        self._tracked: dict[int, _Tracked] = {}
        self._by_channel: dict[str, _Tracked] = {}
        self._listeners: list[Callable[[int], None]] = []
        self.chat = chat_factory(self._on_chat)
        self._chat_was_connected = False
        self._settings_seen: AppSettings | None = None

    def add_listener(self, listener: Callable[[int], None]) -> None:
        """Called with the moment id each time a new moment is saved."""
        self._listeners.append(listener)

    def wake(self) -> None:  # settings are re-read on every tick; nothing to do
        pass

    def status(self) -> dict:
        return {
            "chat_connected": self.chat.connected,
            "sessions": sorted(self._tracked),
        }

    async def run(self) -> None:
        chat_task = asyncio.create_task(self.chat.run())
        log.info("signal service started")
        tick = 0
        try:
            while True:
                try:
                    await self.tick(tick)
                except Exception:
                    log.exception("signal tick failed")
                tick += 1
                await asyncio.sleep(1.0)
        finally:
            chat_task.cancel()
            await asyncio.gather(chat_task, return_exceptions=True)

    async def aclose(self) -> None:
        pass

    async def tick(self, tick: int) -> None:
        now = self._clock()
        if tick % 5 == 0:
            await asyncio.to_thread(self.sync_sessions)
            self.chat.set_channels(set(self._by_channel))
            self._ingest_viewers(now)
        self._watch_chat_connection()
        if tick % 2 == 0:
            await asyncio.to_thread(self.ingest_audio)
        events: list[tuple[str, _Tracked, Candidate]] = []
        if tick % _CLIP_POLL_S == 0:
            events += await self._ingest_clips(now)
        for tracked in list(self._tracked.values()):
            events += [(kind, tracked, c) for kind, c in tracked.detector.evaluate(now)]
        if events:
            new_ids = await asyncio.to_thread(self._persist, events, now)
            for moment_id in new_ids:
                for listener in self._listeners:
                    try:
                        listener(moment_id)
                    except Exception:
                        log.exception("moment listener failed")

    # -- sessions -------------------------------------------------------------------

    def sync_sessions(self) -> None:
        settings = self._settings.get()
        with self._session_factory() as db:
            rows = db.execute(
                select(StreamSession.id, Streamer.id, Streamer.platform, Streamer.username)
                .join(Streamer, Streamer.id == StreamSession.streamer_id)
                .where(StreamSession.ended_at.is_(None), Streamer.enabled.is_(True))
            ).all()
        wanted = {row[0]: row for row in rows}
        for session_id in list(self._tracked):
            if session_id not in wanted:
                del self._tracked[session_id]
        for session_id, streamer_id, platform, username in wanted.values():
            tracked = self._tracked.get(session_id)
            if tracked is None:
                tracked = _Tracked(
                    session_id,
                    streamer_id,
                    platform,
                    username,
                    MomentDetector(detector_config(settings, platform)),
                )
                self._tracked[session_id] = tracked
                if self.chat.connected:
                    self._begin_chat(tracked)
            elif settings != self._settings_seen:
                cfg = detector_config(settings, platform)
                if set(cfg.signals) == set(tracked.detector.cfg.signals):
                    tracked.detector.cfg = cfg
                else:  # a signal was switched on or off: start that session afresh
                    tracked.detector = MomentDetector(cfg)
                    if self.chat.connected:
                        self._begin_chat(tracked)
        self._settings_seen = settings
        self._by_channel = {t.username: t for t in self._tracked.values() if t.platform == "twitch"}

    # -- chat -----------------------------------------------------------------------

    def _on_chat(self, message: ChatMessage) -> None:
        tracked = self._by_channel.get(message.channel)
        if tracked is None:
            return
        tracked.detector.observe("chat", message.ts)
        if is_hype(message.text, message.has_emotes):
            tracked.detector.observe("hype", message.ts)

    def _watch_chat_connection(self) -> None:
        connected = self.chat.connected
        if connected and not self._chat_was_connected:
            # Whatever happened while we were away is unknown, not "silence".
            for tracked in self._by_channel.values():
                tracked.detector.reset("chat")
                tracked.detector.reset("hype")
                self._begin_chat(tracked)
        self._chat_was_connected = connected

    def _begin_chat(self, tracked: _Tracked) -> None:
        now = self._clock()
        tracked.detector.begin("chat", now)
        tracked.detector.begin("hype", now)

    # -- audio ----------------------------------------------------------------------

    def ingest_audio(self) -> None:
        for tracked in list(self._tracked.values()):
            if "audio" not in tracked.detector.cfg.signals:
                continue
            segments = self._store.segments(tracked.session_id)
            current = {seg.path.name for seg in segments}
            tracked.audio_done &= current
            for seg in segments:
                if seg.path.name in tracked.audio_done:
                    continue
                tracked.audio_done.add(seg.path.name)
                if self._clock() - seg.end > 120:
                    continue  # old footage found at startup: not worth analysing
                for offset, level in enumerate(loudness_per_second(seg.path)):
                    tracked.detector.observe("audio", seg.start + offset, level)

    # -- viewers and clips (context) --------------------------------------------------

    def _ingest_viewers(self, now: float) -> None:
        if self._monitor is None:
            return
        for tracked in self._tracked.values():
            count = self._monitor.streamer_status(tracked.streamer_id).viewer_count
            if count is not None:
                tracked.viewers.append((now, count))
                tracked.viewers = [v for v in tracked.viewers if v[0] >= now - _VIEWER_HISTORY_S]

    async def _ingest_clips(self, now: float) -> list[tuple[str, _Tracked, Candidate]]:
        settings = self._settings.get()
        if self._clips is None or settings.detect_clips_count <= 0:
            return []
        events = []
        since = datetime.fromtimestamp(now, UTC) - timedelta(seconds=CLIP_WINDOW_S + 60)
        for tracked in list(self._by_channel.values()):
            try:
                created = await self._clips.recent_clips(tracked.username, since)
            except PlatformError as exc:
                log.info("clip count unavailable for %s: %s", tracked.username, exc)
                continue
            tracked.clip_times = [t.timestamp() for t in created]
            recent = [t for t in tracked.clip_times if t >= now - CLIP_WINDOW_S]
            if len(recent) >= settings.detect_clips_count:
                # Viewers clip just after the moment; a clip covers roughly the 30 s before.
                peak_ts = statistics.median(recent) - 15
                kind, candidate = tracked.detector.trigger(
                    "clips", peak_ts, {"clips": {"z": float(len(recent)), "value": len(recent)}}
                )
                events.append((kind, tracked, candidate))
        return events

    def _context(self, tracked: _Tracked, now: float) -> dict:
        context: dict = {}
        if tracked.viewers:
            current = tracked.viewers[-1][1]
            usual = statistics.median(v for _t, v in tracked.viewers)
            context["viewers"] = {
                "value": current,
                "jump": round(current / usual, 2) if usual else None,
            }
        recent = [t for t in tracked.clip_times if t >= now - CLIP_WINDOW_S]
        if recent:
            context["viewer_clips"] = {"value": len(recent)}
        return context

    # -- saving ---------------------------------------------------------------------

    def _persist(self, events: list[tuple[str, _Tracked, Candidate]], now: float) -> list[int]:
        new_ids: list[int] = []
        with self._session_factory.begin() as db:
            for kind, tracked, candidate in events:
                scores = {
                    **candidate.scores,
                    **self._context(tracked, now),
                    "triggers": list(candidate.triggers),
                }
                if kind == "update" and candidate.ref is not None:
                    moment = db.get(Moment, candidate.ref)
                    if moment is not None:
                        moment.signal_scores = scores
                    continue
                if db.get(StreamSession, tracked.session_id) is None:
                    continue
                moment = Moment(
                    session_id=tracked.session_id,
                    peak_ts=datetime.fromtimestamp(candidate.peak_ts, UTC),
                    signal_scores=scores,
                    status="detected",
                )
                db.add(moment)
                db.flush()
                candidate.ref = moment.id
                write_audit(
                    db,
                    actor="system",
                    action="moment.detect",
                    object_id=f"moment:{moment.id}",
                    details={
                        "session_id": tracked.session_id,
                        "triggers": list(candidate.triggers),
                        "strength": round(candidate.strength, 2),
                    },
                )
                log.info(
                    "moment in %s/%s (%s, z=%.1f)",
                    tracked.platform,
                    tracked.username,
                    "+".join(candidate.triggers),
                    candidate.strength,
                )
                new_ids.append(moment.id)
        return new_ids
