"""Recorder with a real ffmpeg pipeline and a generated live stream."""

from __future__ import annotations

import time

import pytest

from engine.db.models import AuditLog, Streamer, StreamSession, utcnow
from engine.db.session import create_db_engine, make_session_factory, run_migrations
from engine.recorder.service import Recorder
from engine.recorder.store import SegmentStore
from engine.settings import SettingsStore
from tests.media import TestPatternSource, probe


@pytest.fixture
def rig(tmp_path):
    db_engine = create_db_engine(tmp_path / "r.db")
    run_migrations(db_engine)
    factory = make_session_factory(db_engine)
    settings = SettingsStore(tmp_path / "settings.json")
    settings.update({"segment_seconds": 2})
    source = TestPatternSource()
    store = SegmentStore(tmp_path / "buffer", segment_seconds=2)
    recorder = Recorder(factory, store, settings, source=source)
    with factory.begin() as db:
        db.add(Streamer(platform="twitch", username="shroud"))
        db.flush()
        db.add(StreamSession(streamer_id=1, started_at=utcnow()))
    yield type(
        "Rig",
        (),
        dict(
            factory=factory,
            settings=settings,
            source=source,
            store=store,
            recorder=recorder,
            tmp=tmp_path,
        ),
    )
    recorder.stop_all()
    db_engine.dispose()


def wait_for(condition, timeout=20, step=0.2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(step)
    return False


def end_session(rig):
    with rig.factory.begin() as db:
        db.get(StreamSession, 1).ended_at = utcnow()


def test_records_playable_segments_and_stops_with_the_session(rig):
    rig.recorder.reconcile()
    assert rig.recorder.is_recording(1)
    assert rig.source.opened == ["https://www.twitch.tv/shroud"]
    assert wait_for(lambda: len(rig.store.segments(1)) >= 3)

    segs = rig.store.segments(1)
    for seg in segs[:3]:
        info = probe(seg.path)
        assert info["video"] and info["audio"]
        assert 1.5 <= info["duration"] <= 2.6
        assert 1.9 <= seg.duration <= 2.2
    # The timeline is contiguous and ends close to "now", even though the first
    # segments arrived in one burst.
    assert all(abs(b.start - a.end) < 0.05 for a, b in zip(segs, segs[1:], strict=False))
    assert time.time() - 6 <= segs[-1].end <= time.time() + 0.5
    status = rig.recorder.status(1)
    assert status["recording"] and status["buffered_seconds"] >= 5 and status["bytes"] > 0

    with rig.factory() as db:
        assert db.get(StreamSession, 1).buffer_path == str(rig.store.session_dir(1))
        assert [a.action for a in db.query(AuditLog)] == ["recorder.start"]

    end_session(rig)
    rig.recorder.reconcile()
    assert not rig.recorder.is_recording(1)
    count = len(rig.store.segments(1))
    time.sleep(2.5)
    assert len(rig.store.segments(1)) == count  # nothing is still writing
    assert probe(rig.store.segments(1)[-1].path)["video"]  # last segment was closed cleanly


def test_footage_from_a_few_seconds_ago_can_be_cut_out(rig):
    rig.recorder.reconcile()
    assert wait_for(lambda: len(rig.store.segments(1)) >= 4)
    now = time.time()
    copies = rig.store.extract(1, now - 7, now - 4, rig.tmp / "raw" / "m1")
    assert 1 <= len(copies) <= 3
    assert all(probe(c.path)["video"] for c in copies)


def test_rolling_prune_runs_while_recording(rig):
    rig.settings.update({"buffer_minutes": 2})
    rig.recorder.reconcile()
    assert wait_for(lambda: len(rig.store.segments(1)) >= 3)
    # Pretend two minutes have passed: everything finished so far is now too old.
    rig.recorder._clock = lambda: time.time() + 125
    rig.recorder.reconcile()
    assert rig.recorder.last_prune.rolled >= 3
    assert len(rig.store.segments(1)) <= 1
    assert rig.recorder.is_recording(1)


def test_restarts_when_the_stream_drops_and_backs_off(rig):
    rig.source.seconds = 3  # the stream dies after three seconds
    rig.recorder.reconcile()
    assert wait_for(lambda: not rig.recorder.is_recording(1), timeout=15)
    rig.recorder.reconcile()  # notices, schedules a retry
    status = rig.recorder.status(1)
    assert not status["recording"] and status["error"] == "the stream ended"
    rig.recorder.reconcile()
    assert len(rig.source.opened) == 1  # still inside the back-off pause
    rig.recorder._clock = lambda: time.time() + 30
    rig.source.seconds = None
    rig.recorder.reconcile()
    assert len(rig.source.opened) == 2 and rig.recorder.is_recording(1)
    # A second run writes new files next to the first run's, never over them.
    assert wait_for(lambda: len({s.path.name.split("_")[0] for s in rig.store.segments(1)}) == 2)


def test_stream_that_cannot_be_opened_reports_why(rig):
    rig.source.unavailable = True
    rig.recorder.reconcile()
    assert wait_for(lambda: not rig.recorder.is_recording(1), timeout=5)
    rig.recorder.reconcile()
    assert rig.recorder.status(1)["error"] == "could not open the stream (offline)"


def test_disabled_streamer_is_not_recorded(rig):
    with rig.factory.begin() as db:
        db.get(Streamer, 1).enabled = False
    rig.recorder.reconcile()
    assert not rig.recorder.is_recording(1) and rig.source.opened == []


def test_disk_cap_is_audited_once(rig):
    rig.recorder.reconcile()
    assert wait_for(lambda: len(rig.store.segments(1)) >= 3)
    rig.settings._current = rig.settings._current.model_copy(update={"disk_cap_gb": 0.00001})
    rig.recorder.reconcile()
    rig.recorder.reconcile()
    assert rig.recorder.last_prune.total_bytes < 200_000
    with rig.factory() as db:
        actions = [a.action for a in db.query(AuditLog)]
    assert actions.count("recorder.disk_cap") == 1
