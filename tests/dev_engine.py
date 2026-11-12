"""Development-only engine with scripted fake platforms, for working on the UI without
real Twitch/Kick keys. Never shipped; never used by the app.

    uv run python -m tests.dev_engine            # then, in app/:
    VITE_ENGINE_URL=http://127.0.0.1:8765 VITE_ENGINE_TOKEN=<printed token> pnpm dev

Script: twitch "alwayslive" is live from the start; twitch "golive" goes live 15 seconds
after it is added; kick "kicklive" is live; anything containing "ghost" does not exist.
Live channels are "recorded" from a generated test stream, and a moment is "detected"
on each one every DEV_MOMENT_S seconds (default 45), so clips flow into the queue.
"""

from __future__ import annotations

import os
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import keyring
import uvicorn

from engine.api.app import AppContext, create_app
from engine.config import Config
from engine.db.session import create_db_engine, run_migrations
from engine.keychain import SecretStore
from engine.logging_setup import SecretRedactor, setup_logging
from engine.monitor.platforms import LiveInfo, MissingCredentials, Snapshot
from engine.monitor.service import Monitor
from engine.recorder.service import Recorder
from engine.recorder.store import SegmentStore
from engine.render.captions import Word
from engine.render.pipeline import ClipPipeline
from tests.fake_keyring import MemoryKeyring
from tests.media import TestPatternSource

TOKEN = "dev-" + "0" * 60
PORT = 8765


class ScriptedPlatform:
    def __init__(self, platform: str, need_keys: bool = False) -> None:
        self.platform = platform
        self.need_keys = need_keys
        self.first_seen: dict[str, float] = {}
        self.started: dict[str, datetime] = {}

    async def check(self, usernames, verify):
        if self.need_keys:
            raise MissingCredentials(f"{self.platform} keys are not set")
        snap = Snapshot()
        for name in usernames:
            first = self.first_seen.setdefault(name, time.monotonic())
            if "ghost" in name:
                if name in verify:
                    snap.missing.add(name)
                continue
            if name in verify:
                snap.found[name] = name.title()
            is_live = "live" in name and ("golive" not in name or time.monotonic() - first >= 15)
            if is_live:
                started = self.started.setdefault(name, datetime.now(UTC).replace(microsecond=0))
                snap.live[name] = LiveInfo(
                    started_at=started,
                    title="GTA VI launch day - first look",
                    game="Grand Theft Auto VI",
                    viewer_count=48213,
                )
        return snap

    async def aclose(self) -> None:
        pass


class ScriptedTranscriber:
    def transcribe(self, audio):
        lines = "no way he actually pulled that off look at this that was insane".split()
        return [Word(text, 14 + i * 0.45, 14 + i * 0.45 + 0.4) for i, text in enumerate(lines)]


class ScriptedClassifier:
    """Stands in for the Claude policy classifier: flags every third clip it sees."""

    def __init__(self) -> None:
        self.seen = 0

    def check(self, video, duration, transcript):
        from engine.policy.text import Finding

        self.seen += 1
        if self.seen % 3 == 0:
            return [
                Finding(
                    "personal_info", "flag", "possibly: personal information is shown or spoken"
                ),
                Finding("classifier_notes", "flag", "A chat window with a full name is visible."),
            ]
        return []


class ScriptedDetector:
    """Stands in for the signal service: marks a moment on every live session on a timer."""

    def __init__(self, session_factory, pipeline, every_s: float) -> None:
        self._session_factory = session_factory
        self._pipeline = pipeline
        self._every_s = every_s

    async def run(self) -> None:
        import asyncio
        from datetime import timedelta

        from sqlalchemy import select

        from engine.db.models import Moment, StreamSession, utcnow

        while True:
            await asyncio.sleep(self._every_s)
            with self._session_factory.begin() as db:
                for stream in db.scalars(
                    select(StreamSession).where(StreamSession.ended_at.is_(None))
                ):
                    db.add(
                        Moment(
                            session_id=stream.id,
                            peak_ts=utcnow() - timedelta(seconds=12),
                            signal_scores={
                                "triggers": ["chat", "hype"],
                                "chat": {"z": 5.4, "value": 96, "baseline": 31},
                            },
                        )
                    )
            self._pipeline.wake()

    async def aclose(self) -> None:
        pass

    def wake(self) -> None:
        pass


def main() -> None:
    keyring.set_keyring(MemoryKeyring())
    data_dir = Path(os.environ.get("CLIPPER_DATA_DIR") or tempfile.mkdtemp(prefix="clipper-dev-"))
    config = Config(data_dir=data_dir)
    config.ensure_dirs()
    redactor = SecretRedactor()
    setup_logging(config, redactor)
    db_engine = create_db_engine(config.db_path)
    run_migrations(db_engine)
    ctx = AppContext(config=config, token=TOKEN, db_engine=db_engine, secrets=SecretStore(redactor))
    ctx.monitor = Monitor(
        ctx.session_factory,
        {
            "twitch": ScriptedPlatform("twitch"),
            "kick": ScriptedPlatform("kick", need_keys=bool(os.environ.get("DEV_KICK_NO_KEYS"))),
        },
        poll_interval_s=float(os.environ.get("DEV_POLL_S", "20")),
    )
    store = SegmentStore(config.buffer_dir)
    ctx.recorder = Recorder(
        ctx.session_factory,
        store,
        ctx.settings,
        source=TestPatternSource(size="1280x720", bitrate="1500k", pattern="mandelbrot"),
    )
    ctx.monitor.add_listener(ctx.recorder.on_session_event)
    from engine.control.spend import SpendLedger

    ctx.pipeline = ClipPipeline(
        ctx.session_factory,
        config,
        ctx.settings,
        store,
        SpendLedger(ctx.session_factory),
        transcriber=ScriptedTranscriber(),
        is_recording=ctx.recorder.is_recording,
    )
    from engine.policy.gate import PolicyGate

    ctx.gate = PolicyGate(ctx.session_factory, ctx.settings, classifier=ScriptedClassifier())
    ctx.pipeline.add_listener(ctx.gate.review)
    ctx.signals = ScriptedDetector(
        ctx.session_factory, ctx.pipeline, float(os.environ.get("DEV_MOMENT_S", "45"))
    )
    print(f"dev engine on http://127.0.0.1:{PORT}  token: {TOKEN}", flush=True)
    uvicorn.run(create_app(ctx), host="127.0.0.1", port=PORT, log_config=None, access_log=False)


if __name__ == "__main__":
    main()
