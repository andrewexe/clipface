"""Audio loudness and the signal service that turns live signals into saved moments."""

from __future__ import annotations

import os
import subprocess
import time
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from engine.db.models import AuditLog, Moment, Streamer, StreamSession, utcnow
from engine.db.session import create_db_engine, make_session_factory, run_migrations
from engine.media.ffmpeg import ffmpeg_path
from engine.recorder.store import SegmentStore
from engine.settings import SettingsStore
from engine.signals.audio import loudness_per_second
from engine.signals.service import SignalService, detector_config
from engine.signals.twitch_chat import ChatMessage

pytestmark = pytest.mark.anyio


def make_audio(path, parts):
    """parts: list of (seconds, volume 0..1) of a 440 Hz tone, one after another."""
    inputs, filters = [], []
    for i, (seconds, volume) in enumerate(parts):
        inputs += ["-f", "lavfi", "-t", str(seconds), "-i", "sine=frequency=440:sample_rate=48000"]
        filters.append(f"[{i}:a]volume={volume}[a{i}]")
    concat = "".join(f"[a{i}]" for i in range(len(parts))) + f"concat=n={len(parts)}:v=0:a=1[out]"
    subprocess.run(
        [
            ffmpeg_path(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            *inputs,
            "-filter_complex",
            ";".join(filters) + ";" + concat,
            "-map",
            "[out]",
            "-c:a",
            "aac",
            "-f",
            "mpegts",
            str(path),
        ],
        check=True,
    )


def test_loudness_per_second_tracks_volume(tmp_path):
    path = tmp_path / "a.ts"
    make_audio(path, [(3, 0.02), (2, 0.8), (3, 0.02)])
    levels = loudness_per_second(path)
    assert len(levels) == 8
    quiet, loud = levels[1], levels[3]
    assert loud - quiet > 25  # 0.8 vs 0.02 is 32 dB
    assert all(-90 <= level <= 0 for level in levels)


def test_loudness_of_missing_or_broken_file_is_empty(tmp_path):
    assert loudness_per_second(tmp_path / "nope.ts") == []
    (tmp_path / "junk.ts").write_bytes(b"not video")
    assert loudness_per_second(tmp_path / "junk.ts") == []


class FakeChat:
    def __init__(self, on_message):
        self.on_message = on_message
        self.connected = True
        self.channels: set[str] = set()

    def set_channels(self, channels):
        self.channels = set(channels)

    async def run(self):
        pass


class FakeClips:
    def __init__(self):
        self.times: list[datetime] = []
        self.asked: list[str] = []

    async def recent_clips(self, login, since):
        self.asked.append(login)
        return [t for t in self.times if t >= since]


class Clock:
    def __init__(self, start):
        self.t = float(start)

    def __call__(self):
        return self.t


@pytest.fixture
def rig(tmp_path):
    db_engine = create_db_engine(tmp_path / "s.db")
    run_migrations(db_engine)
    factory = make_session_factory(db_engine)
    with factory.begin() as db:
        db.add(Streamer(platform="twitch", username="shroud"))
        db.add(Streamer(platform="kick", username="xqc"))
        db.flush()
        db.add(StreamSession(streamer_id=1, started_at=utcnow()))
        db.add(StreamSession(streamer_id=2, started_at=utcnow()))
    settings = SettingsStore(tmp_path / "settings.json")
    store = SegmentStore(tmp_path / "buffer")
    clock = Clock(1_800_000_000)
    clips = FakeClips()
    service = SignalService(
        factory, settings, store, clips=clips, chat_factory=FakeChat, clock=clock
    )
    yield type(
        "Rig",
        (),
        dict(
            factory=factory,
            settings=settings,
            store=store,
            clock=clock,
            clips=clips,
            service=service,
            tmp=tmp_path,
        ),
    )
    db_engine.dispose()


def moments(rig):
    with rig.factory() as db:
        return db.scalars(select(Moment).order_by(Moment.id)).all()


async def run_seconds(
    rig, seconds, chat_per_second=0, hype_per_second=0, channel="shroud", start_tick=0
):
    """Advance the fake clock one second at a time, feeding chat as we go."""
    for i in range(seconds):
        for n in range(chat_per_second):
            text = "KEKW" if n < hype_per_second else "hello there"
            rig.service.chat.on_message(
                ChatMessage(channel, rig.clock.t + n / (chat_per_second + 1), text, False)
            )
        rig.clock.t += 1
        await rig.service.tick(start_tick + i)
    return start_tick + seconds


def test_kick_sessions_get_audio_only_and_twitch_gets_chat():
    from engine.settings import AppSettings

    assert set(detector_config(AppSettings(), "twitch").signals) == {"chat", "hype", "audio"}
    assert set(detector_config(AppSettings(), "kick").signals) == {"audio"}
    quiet = AppSettings(detect_audio=False)
    assert set(detector_config(quiet, "kick").signals) == set()
    strict = detector_config(AppSettings(detect_z_threshold=5, detect_cooldown_s=120), "twitch")
    assert strict.signals["chat"].threshold == 5 and strict.cooldown_s == 120


async def test_chat_spike_becomes_a_saved_moment(rig):
    heard = []
    rig.service.add_listener(heard.append)
    tick = await run_seconds(rig, 200, chat_per_second=3)
    assert rig.service.chat.channels == {"shroud"}  # only Twitch channels are joined
    assert moments(rig) == []
    spike_at = rig.clock.t
    tick = await run_seconds(rig, 10, chat_per_second=25, hype_per_second=15, start_tick=tick)
    await run_seconds(rig, 40, chat_per_second=3, start_tick=tick)

    (moment,) = moments(rig)
    assert moment.session_id == 1 and moment.status == "detected"
    assert abs(moment.peak_ts.timestamp() - (spike_at + 5)) <= 8
    scores = moment.signal_scores
    assert set(scores["triggers"]) == {"chat", "hype"}
    assert scores["chat"]["z"] >= 3 and scores["chat"]["value"] > scores["chat"]["baseline"]
    assert heard == [moment.id]
    with rig.factory() as db:
        (entry,) = db.scalars(select(AuditLog).where(AuditLog.action == "moment.detect")).all()
    assert entry.actor == "system" and entry.object_id == f"moment:{moment.id}"


async def test_chat_for_untracked_channels_is_ignored(rig):
    tick = await run_seconds(rig, 200, chat_per_second=3, channel="someone_else")
    await run_seconds(rig, 30, chat_per_second=30, channel="someone_else", start_tick=tick)
    assert moments(rig) == []


async def test_chat_reconnect_does_not_create_a_moment(rig):
    tick = await run_seconds(rig, 200, chat_per_second=1)
    rig.service.chat.connected = False
    tick = await run_seconds(rig, 300, start_tick=tick)  # outage: nothing received
    rig.service.chat.connected = True
    await run_seconds(rig, 60, chat_per_second=20, start_tick=tick)  # busy chat on return
    assert moments(rig) == []


async def test_ended_sessions_stop_being_tracked(rig):
    await run_seconds(rig, 6)
    assert rig.service.status()["sessions"] == [1, 2]
    with rig.factory.begin() as db:
        db.get(StreamSession, 1).ended_at = utcnow()
    await run_seconds(rig, 6)
    assert rig.service.status()["sessions"] == [2]
    assert rig.service.chat.channels == set()


async def test_audio_spike_on_kick_becomes_a_moment(rig):
    # Thirty 10-second segments of quiet tone, then one loud one, listed as ffmpeg would.
    folder = rig.store.session_dir(2)
    folder.mkdir(parents=True)
    quiet, loud = rig.tmp / "quiet.ts", rig.tmp / "loud.ts"
    make_audio(quiet, [(10, 0.01)])
    make_audio(loud, [(3, 0.01), (5, 0.9), (2, 0.01)])
    base = rig.clock.t
    for i in range(32):
        target = folder / f"100_{i:06d}.ts"
        target.write_bytes((loud if i == 30 else quiet).read_bytes())
        end = base + 10 * (i + 1)
        os.utime(target, (end, end))
        with (folder / "100.csv").open("a") as handle:
            handle.write(f"{target.name},{i * 10:.6f},{(i + 1) * 10:.6f}\n")
    rig.clock.t = base + 330
    for tick in range(0, 12):
        rig.clock.t += 1
        await rig.service.tick(tick)

    (moment,) = moments(rig)
    assert moment.session_id == 2
    assert moment.signal_scores["triggers"] == ["audio"]
    assert abs(moment.peak_ts.timestamp() - (base + 305)) <= 8


async def test_viewer_clips_trigger_and_merge(rig):
    now = datetime.fromtimestamp(rig.clock.t, UTC)
    rig.clips.times = [now.replace(microsecond=0)] * 4
    await rig.service.tick(0)
    (moment,) = moments(rig)
    assert moment.signal_scores["triggers"] == ["clips"]
    assert moment.signal_scores["viewer_clips"] == {"value": 4}
    assert rig.clips.asked == ["shroud"]  # Twitch only

    rig.clock.t += 30
    await rig.service.tick(30)  # same clips again: still one moment
    assert len(moments(rig)) == 1

    rig.settings.update({"detect_clips_count": 0})
    rig.clips.asked.clear()
    rig.clock.t += 30
    await rig.service.tick(60)
    assert rig.clips.asked == []  # switched off


async def test_real_time_smoke(rig):
    """The service loop itself: start it, let it tick for real, stop it cleanly."""
    import asyncio

    rig.service._clock = time.time
    task = asyncio.create_task(rig.service.run())
    await asyncio.sleep(1.3)
    assert rig.service.status()["sessions"] == [1, 2]
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
