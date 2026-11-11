"""Live monitor: sessions open and close as streamers go live and offline."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from engine.db.models import AuditLog, Streamer, StreamSession
from engine.db.session import create_db_engine, make_session_factory, run_migrations
from engine.monitor.platforms import (
    LiveInfo,
    MissingCredentials,
    PlatformAuthError,
    PlatformError,
    Snapshot,
)
from engine.monitor.service import Monitor

pytestmark = pytest.mark.anyio

T0 = datetime(2026, 11, 19, 20, 0, tzinfo=UTC)


class FakePlatform:
    """Stands in for a platform API. Set `live`, `known`, or `error` between polls."""

    def __init__(self):
        self.live: dict[str, LiveInfo] = {}
        self.known: dict[str, str] = {}
        self.error: Exception | None = None
        self.calls: list[tuple[list[str], set[str]]] = []

    async def check(self, usernames, verify):
        self.calls.append((list(usernames), set(verify)))
        if self.error:
            raise self.error
        snap = Snapshot(live={n: i for n, i in self.live.items() if n in usernames})
        snap.found = {n: self.known[n] for n in verify if n in self.known}
        snap.missing = {n for n in verify if n not in self.known}
        return snap

    async def aclose(self):
        pass


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now

    def tick(self, seconds=20):
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def world(tmp_path):
    db_engine = create_db_engine(tmp_path / "m.db")
    run_migrations(db_engine)
    factory = make_session_factory(db_engine)
    twitch, kick, clock = FakePlatform(), FakePlatform(), Clock()
    twitch.known = {"shroud": "shroud"}
    kick.known = {"xqc": "xQc"}
    monitor = Monitor(factory, {"twitch": twitch, "kick": kick}, now=clock)
    with factory.begin() as db:
        db.add(Streamer(platform="twitch", username="shroud"))
        db.add(Streamer(platform="kick", username="xqc"))
    yield type(
        "World", (), dict(factory=factory, twitch=twitch, kick=kick, clock=clock, monitor=monitor)
    )
    db_engine.dispose()


def sessions(world, username=None):
    with world.factory() as db:
        query = select(StreamSession).join(Streamer).order_by(StreamSession.id)
        if username:
            query = query.where(Streamer.username == username)
        return db.scalars(query).all()


def audit_actions(world):
    with world.factory() as db:
        return [row.action for row in db.scalars(select(AuditLog).order_by(AuditLog.id))]


def live(started=T0, title="day one", game="GTA VI"):
    return LiveInfo(started_at=started, title=title, game=game, viewer_count=100)


async def test_offline_streamers_get_no_session(world):
    await world.monitor.poll_once()
    assert sessions(world) == []
    assert world.monitor.platform_status()["twitch"]["state"] == "ok"


async def test_going_live_opens_one_session_with_details(world):
    await world.monitor.poll_once()
    world.twitch.live["shroud"] = live()
    await world.monitor.poll_once()
    await world.monitor.poll_once()  # still live: no second session
    (stream,) = sessions(world)
    assert stream.ended_at is None
    assert (stream.started_at, stream.title, stream.game) == (T0, "day one", "GTA VI")
    assert audit_actions(world) == ["session.start"]


async def test_title_changes_update_the_open_session(world):
    world.twitch.live["shroud"] = live()
    await world.monitor.poll_once()
    world.twitch.live["shroud"] = live(title="heist time", game="GTA Online")
    await world.monitor.poll_once()
    (stream,) = sessions(world)
    assert (stream.title, stream.game) == ("heist time", "GTA Online")


async def test_going_offline_closes_after_grace_not_on_one_blip(world):
    world.twitch.live["shroud"] = live()
    await world.monitor.poll_once()

    info = world.twitch.live.pop("shroud")  # one empty response: an API blip
    world.clock.tick()
    await world.monitor.poll_once()
    assert sessions(world)[0].ended_at is None

    world.twitch.live["shroud"] = info  # back: still the same session
    world.clock.tick()
    await world.monitor.poll_once()
    assert len(sessions(world)) == 1 and sessions(world)[0].ended_at is None

    del world.twitch.live["shroud"]  # really offline: two misses in a row
    world.clock.tick()
    await world.monitor.poll_once()
    world.clock.tick()
    await world.monitor.poll_once()
    (stream,) = sessions(world)
    assert stream.ended_at == world.clock.now
    assert audit_actions(world) == ["session.start", "session.end"]


async def test_new_broadcast_closes_old_session_and_opens_new(world):
    world.twitch.live["shroud"] = live(started=T0)
    await world.monitor.poll_once()
    restarted = T0 + timedelta(hours=3)
    world.twitch.live["shroud"] = live(started=restarted, title="part two")
    await world.monitor.poll_once()
    first, second = sessions(world)
    assert first.ended_at == restarted
    assert second.started_at == restarted and second.ended_at is None


async def test_restart_keeps_the_same_session_for_the_same_stream(world):
    world.twitch.live["shroud"] = live()
    await world.monitor.poll_once()
    reborn = Monitor(world.factory, {"twitch": world.twitch, "kick": world.kick}, now=world.clock)
    await reborn.poll_once()
    assert len(sessions(world)) == 1 and sessions(world)[0].ended_at is None


async def test_platforms_are_independent(world):
    world.twitch.error = PlatformError("twitch returned 503")
    world.kick.live["xqc"] = live()
    await world.monitor.poll_once()
    assert [s.streamer_id for s in sessions(world)] == [2]
    status = world.monitor.platform_status()
    assert status["twitch"]["state"] == "error" and status["kick"]["state"] == "ok"


async def test_api_errors_never_close_a_live_session(world):
    world.twitch.live["shroud"] = live()
    await world.monitor.poll_once()
    world.twitch.error = PlatformError("timeout")
    for _ in range(5):
        world.clock.tick()
        await world.monitor.poll_once()
    assert sessions(world)[0].ended_at is None
    world.twitch.error = None
    await world.monitor.poll_once()
    assert world.monitor.platform_status()["twitch"]["state"] == "ok"
    assert len(sessions(world)) == 1


async def test_credential_states(world):
    world.twitch.error = MissingCredentials("twitch keys are not set")
    world.kick.error = PlatformAuthError("kick rejected the client ID or secret")
    await world.monitor.poll_once()
    status = world.monitor.platform_status()
    assert status["twitch"]["state"] == "needs_keys"
    assert status["kick"]["state"] == "bad_keys"
    assert sessions(world) == []


async def test_rate_limit_backs_off(world):
    world.twitch.error = PlatformError("rate limit", retry_after_s=60)
    await world.monitor.poll_once()
    world.twitch.error = None
    await world.monitor.poll_once()
    assert len(world.twitch.calls) == 1  # second poll skipped twitch


async def test_verifies_new_streamers_once_and_flags_missing(world):
    with world.factory.begin() as db:
        db.add(Streamer(platform="twitch", username="typo_name"))
    await world.monitor.poll_once()
    await world.monitor.poll_once()
    assert world.twitch.calls[0][1] == {"shroud", "typo_name"}
    assert world.twitch.calls[1][1] == set()
    with world.factory() as db:
        by_name = {s.username: s for s in db.scalars(select(Streamer))}
    assert world.monitor.streamer_status(by_name["typo_name"].id).exists is False
    assert world.monitor.streamer_status(by_name["shroud"].id).exists is True
    assert by_name["xqc"].display_name == "xQc"


async def test_disabled_streamers_are_not_polled_and_sessions_close(world):
    world.twitch.live["shroud"] = live()
    await world.monitor.poll_once()
    with world.factory.begin() as db:
        db.scalars(select(Streamer).where(Streamer.username == "shroud")).one().enabled = False
    world.clock.tick()
    await world.monitor.poll_once()
    assert sessions(world)[0].ended_at == world.clock.now
    assert world.twitch.calls[-1][0] == ["shroud"]  # the last twitch call predates the disable
    assert len(world.twitch.calls) == 1


async def test_listeners_hear_start_and_end(world):
    heard = []
    world.monitor.add_listener(lambda kind, session_id: heard.append((kind, session_id)))
    world.monitor.add_listener(lambda kind, session_id: 1 / 0)  # a bad listener can't break it
    world.twitch.live["shroud"] = live()
    await world.monitor.poll_once()
    del world.twitch.live["shroud"]
    await world.monitor.poll_once()
    await world.monitor.poll_once()
    assert heard == [("start", 1), ("end", 1)]


async def test_marked_live_within_60_seconds(world):
    """Phase 1 check, run in real time: default 20s polling, stream starts mid-interval."""
    monitor = Monitor(
        world.factory, {"twitch": world.twitch, "kick": world.kick}, poll_interval_s=2.0
    )
    task = asyncio.create_task(monitor.run())
    try:
        await asyncio.sleep(0.5)  # first poll done; streamer offline
        assert sessions(world) == []
        went_live = time.monotonic()
        world.twitch.live["shroud"] = live()
        while not sessions(world):
            assert time.monotonic() - went_live < 5, "not marked live in time"
            await asyncio.sleep(0.05)
        elapsed = time.monotonic() - went_live
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert elapsed <= monitor.poll_interval_s + 0.5
    # With the shipped interval the worst case is one interval plus one request.
    from engine.monitor.service import DEFAULT_POLL_INTERVAL_S

    assert DEFAULT_POLL_INTERVAL_S + 10 <= 60


async def test_wake_polls_immediately(world):
    monitor = Monitor(
        world.factory, {"twitch": world.twitch, "kick": world.kick}, poll_interval_s=60
    )
    task = asyncio.create_task(monitor.run())
    try:
        await asyncio.sleep(0.3)
        before = len(world.twitch.calls)
        await asyncio.to_thread(monitor.wake)  # called from another thread, as the API does
        await asyncio.sleep(0.3)
        assert len(world.twitch.calls) == before + 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
