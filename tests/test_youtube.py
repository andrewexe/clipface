"""YouTube publishing against a fake server. Includes the Phase 8 check: a forced retry
never creates a duplicate upload."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import keyring
import pytest
from sqlalchemy import select

from engine.db.models import (
    Account,
    AuditLog,
    Clip,
    Metric,
    Moment,
    PublishJob,
    Streamer,
    StreamSession,
)
from engine.db.session import create_db_engine, make_session_factory, run_migrations
from engine.keychain import SecretStore
from engine.logging_setup import SecretRedactor
from engine.publish import youtube
from engine.publish.service import MAX_ATTEMPTS, Publisher, idempotency_key
from engine.publish.youtube import (
    YouTubeClient,
    YouTubePublisher,
    authorization_url,
    clean_metadata,
    new_pkce,
)
from engine.settings import SettingsStore
from tests.fake_keyring import MemoryKeyring
from tests.fake_youtube import CLIENT, REFRESH, FakeYouTube

T0 = datetime(2026, 11, 19, 14, 0, tzinfo=UTC)  # 14:00, inside the posting window
REF = "acct_youtube_0123456789abcdef"


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


@pytest.fixture
def rig(tmp_path):
    previous = keyring.get_keyring()
    keyring.set_keyring(MemoryKeyring())
    db_engine = create_db_engine(tmp_path / "y.db")
    run_migrations(db_engine)
    factory = make_session_factory(db_engine)
    settings = SettingsStore(tmp_path / "settings.json")
    secrets = SecretStore(SecretRedactor())
    secrets.set(REF, REFRESH)
    video = tmp_path / "clip_1.mp4"
    video.write_bytes(bytes(range(256)) * 20)  # 5,120 bytes
    with factory.begin() as db:
        db.add(Streamer(platform="twitch", username="shroud", display_name="shroud",
                        permission_status="granted", program_rules={"required_tags": ["#ShroudClips"]}))  # fmt: skip
        db.add(Account(platform="youtube", handle="My Clips", keychain_ref=REF, daily_cap=3))
        db.flush()
        db.add(StreamSession(streamer_id=1, started_at=T0))
        db.flush()
        db.add(Moment(session_id=1, peak_ts=T0, status="clipped"))
        db.flush()
        db.add(Clip(moment_id=1, start_ts=T0, end_ts=T0, title="He <actually> did it", caption="wow",
                    status="approved", file_path=str(video), policy_result={"verdict": "clean"}))  # fmt: skip
    clock = Clock()
    fake = FakeYouTube(now=clock)
    client = YouTubeClient(lambda: CLIENT, transport=httpx.MockTransport(fake))
    alerts: list[str] = []
    publisher = Publisher(
        factory, settings, secrets, {"youtube": YouTubePublisher(client, clock)},
        now=clock, uniform=lambda low, high: (low + high) / 2, tz=UTC,
    )  # fmt: skip
    publisher.on_alert(alerts.append)

    def job(job_id=1):
        with factory() as db:
            return db.get(PublishJob, job_id)

    def audit():
        with factory() as db:
            return [a.action for a in db.scalars(select(AuditLog).order_by(AuditLog.id))]

    def add_clip(title="Another one"):
        with factory.begin() as db:
            moment = Moment(session_id=1, peak_ts=T0, status="clipped")
            db.add(moment)
            db.flush()
            clip = Clip(moment_id=moment.id, start_ts=T0, end_ts=T0, title=title, status="approved",
                        file_path=str(video), policy_result={"verdict": "clean"})  # fmt: skip
            db.add(clip)
            db.flush()
            return clip.id

    yield SimpleNamespace(factory=factory, settings=settings, secrets=secrets, fake=fake, clock=clock,
                          publisher=publisher, client=client, alerts=alerts, job=job, audit=audit,
                          add_clip=add_clip, video=video)  # fmt: skip
    client.close()
    db_engine.dispose()
    keyring.set_keyring(previous)


def due(rig):
    """Move the clock past every scheduled job and run the publisher once."""
    rig.clock.advance(minutes=11)
    rig.publisher.run_due()


# -- client ---------------------------------------------------------------------------


def test_authorization_url_uses_pkce_loopback_and_offline_access():
    pkce = new_pkce()
    url = authorization_url("cid", "http://127.0.0.1:5123", pkce)
    query = parse_qs(urlsplit(url).query)
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert query["code_challenge_method"] == ["S256"] and query["code_challenge"] == [
        pkce.challenge
    ]
    assert query["redirect_uri"] == ["http://127.0.0.1:5123"] and query["state"] == [pkce.state]
    assert query["access_type"] == ["offline"] and query["response_type"] == ["code"]
    assert "youtube.upload" in query["scope"][0]
    assert pkce.verifier not in url  # only the challenge leaves the machine
    assert new_pkce().state != pkce.state and len(pkce.verifier) >= 43


def test_metadata_is_cleaned_for_youtube():
    title, description = clean_metadata(
        "  He <actually>   did it " + "x" * 200, "see <b>this</b>" + "y" * 6000
    )
    assert title.startswith("He actually did it") and len(title) <= 100
    assert "<" not in description and len(description) <= 4900
    assert clean_metadata("<>", "")[0] == "Clip"


def test_access_token_is_cached_and_refreshed_on_401(rig):
    assert rig.client.channel(REFRESH)["title"] == "My Clips"
    rig.client.channel(REFRESH)
    token_calls = [c for c in rig.fake.calls if c.url.path == "/token"]
    assert len(token_calls) == 1
    rig.fake.access_serial += 1  # the server rotated: our cached token is now stale
    assert rig.client.channel(REFRESH)["uploads"] == "UU1"
    assert len([c for c in rig.fake.calls if c.url.path == "/token"]) == 2


# -- scheduling -----------------------------------------------------------------------


def test_schedule_creates_one_job_per_account_and_is_idempotent(rig):
    assert rig.publisher.schedule_clip(1) == [1]
    assert rig.publisher.schedule_clip(1) == []  # asked again: nothing new
    job = rig.job()
    assert job.status == "scheduled" and job.idempotency_key == idempotency_key(1, 1)
    assert T0 + timedelta(minutes=1) <= job.scheduled_at <= T0 + timedelta(minutes=10)
    with rig.factory.begin() as db:
        db.add(Account(platform="youtube", handle="Second", keychain_ref=REF.replace("0", "1", 1)))
    assert len(rig.publisher.schedule_clip(1)) == 1  # only the new account gets a job
    assert rig.audit().count("publish.schedule") == 2


@pytest.mark.parametrize("status", ["none", "asked"])
def test_never_scheduled_without_the_streamers_permission(rig, status):
    with rig.factory.begin() as db:
        db.get(Streamer, 1).permission_status = status
    assert rig.publisher.schedule_clip(1) == []


def test_only_approved_clips_are_scheduled(rig):
    for status in ("pending", "rejected", "removed"):
        with rig.factory.begin() as db:
            db.get(Clip, 1).status = status
        assert rig.publisher.schedule_clip(1) == []


def test_posts_are_spaced_by_a_random_gap(rig):
    gaps = iter([5, 37, 81])
    rig.publisher._uniform = lambda low, high: next(gaps)
    first = rig.job(rig.publisher.schedule_clip(1)[0]).scheduled_at
    second = rig.job(rig.publisher.schedule_clip(rig.add_clip())[0]).scheduled_at
    third = rig.job(rig.publisher.schedule_clip(rig.add_clip())[0]).scheduled_at
    assert first == T0 + timedelta(minutes=5)
    assert second - first == timedelta(minutes=37) and third - second == timedelta(minutes=81)
    assert 20 <= 37 <= 90  # the gaps asked for were inside the configured range


def test_daily_cap_pushes_posts_to_the_next_day(rig):
    with rig.factory.begin() as db:
        db.get(Account, 1).daily_cap = 2
    days = []
    for _ in range(5):
        job = rig.job(rig.publisher.schedule_clip(rig.add_clip())[0])
        days.append(job.scheduled_at.date().isoformat())
    assert days == ["2026-11-19", "2026-11-19", "2026-11-20", "2026-11-20", "2026-11-21"]


def test_posts_stay_inside_the_time_window(rig):
    rig.clock.now = T0.replace(hour=23, minute=30)  # after the 9:00 to 23:00 window
    late = rig.job(rig.publisher.schedule_clip(1)[0]).scheduled_at
    assert late.date().isoformat() == "2026-11-20" and late.hour == 9
    rig.clock.now = T0.replace(hour=3)
    early = rig.job(rig.publisher.schedule_clip(rig.add_clip())[0]).scheduled_at
    assert 9 <= early.hour < 23


# -- posting --------------------------------------------------------------------------


def test_posts_when_due_with_credit_tags_and_private_by_default(rig):
    rig.publisher.schedule_clip(1)
    rig.publisher.run_due()
    assert rig.fake.upload_count() == 0  # not due yet
    due(rig)

    (video,) = rig.fake.videos.values()
    assert video["title"] == "He actually did it" and video["privacy"] == "private"
    assert video["size"] == 5120
    assert "#ShroudClips" in video["description"]
    assert "Clip from shroud: https://www.twitch.tv/shroud" in video["description"]
    job = rig.job()
    assert job.status == "succeeded" and job.platform_post_id == "vid001"
    with rig.factory() as db:
        assert db.get(Clip, 1).status == "published"
    assert rig.audit()[-1] == "clip.publish"
    rig.publisher.run_due()
    assert rig.fake.upload_count() == 1  # a finished job is never run again


def test_privacy_setting_is_used(rig):
    rig.settings.update({"youtube_privacy": "unlisted"})
    rig.publisher.schedule_clip(1)
    due(rig)
    assert next(iter(rig.fake.videos.values()))["privacy"] == "unlisted"


def test_kill_switch_and_paused_account_stop_posting(rig):
    rig.publisher.schedule_clip(1)
    rig.settings.update({"publishing_paused": True})
    due(rig)
    assert rig.fake.upload_count() == 0 and rig.job().status == "scheduled"
    rig.settings.update({"publishing_paused": False})
    with rig.factory.begin() as db:
        db.get(Account, 1).paused = True
    rig.publisher.run_due()
    assert rig.fake.upload_count() == 0
    with rig.factory.begin() as db:
        db.get(Account, 1).paused = False
    rig.publisher.run_due()
    assert rig.fake.upload_count() == 1


@pytest.mark.parametrize(
    ("change", "why"),
    [
        (
            lambda db, rig: setattr(db.get(Streamer, 1), "permission_status", "none"),
            "permission was withdrawn",
        ),
        (lambda db, rig: setattr(db.get(Streamer, 1), "enabled", False), "streamer is paused"),
        (lambda db, rig: setattr(db.get(Clip, 1), "status", "rejected"), "no longer approved"),
        (
            lambda db, rig: setattr(db.get(Clip, 1), "policy_result", {"verdict": "block"}),
            "safety checks",
        ),
        (lambda db, rig: rig.video.unlink(), "file is missing"),
    ],
)
def test_everything_is_checked_again_at_the_moment_of_posting(rig, change, why):
    rig.publisher.schedule_clip(1)
    with rig.factory.begin() as db:
        change(db, rig)
    due(rig)
    job = rig.job()
    assert job.status == "cancelled" and why in job.error
    assert rig.fake.upload_count() == 0 and rig.audit()[-1] == "publish.cancel"


def test_daily_cap_is_also_enforced_when_posting(rig):
    with rig.factory.begin() as db:
        db.get(Account, 1).daily_cap = 5
    ids = [rig.publisher.schedule_clip(clip)[0] for clip in (1, rig.add_clip(), rig.add_clip())]
    with rig.factory.begin() as db:
        db.get(Account, 1).daily_cap = 1  # lowered after the jobs were scheduled
    rig.clock.advance(hours=5)
    rig.publisher.run_due()
    assert rig.fake.upload_count() == 1
    statuses = [rig.job(i).status for i in ids]
    assert statuses.count("succeeded") == 1 and statuses.count("scheduled") == 2
    assert all(rig.job(i).scheduled_at.date().isoformat() == "2026-11-20" for i in ids[1:])


# -- the Phase 8 check: retries never duplicate -------------------------------------------


@pytest.mark.parametrize(
    "faults",
    [
        ["drop_after_complete"],  # the video exists, but we never heard back
        ["drop_mid_upload"],  # half the bytes arrived
        ["drop_mid_upload", "drop_mid_upload", "drop_after_complete"],
        ["expire_after_complete"],  # finished, reply lost, and the session is gone too
        ["server_error"],
        ["drop_mid_upload", "expire_session"],  # half sent, then the session vanished
    ],
)
def test_forced_retries_never_create_a_duplicate_upload(rig, faults, monkeypatch):
    monkeypatch.setattr(youtube, "CHUNK", 1024)  # five chunks, so an upload can break mid-way
    rig.fake.faults = list(faults)
    rig.publisher.schedule_clip(1)
    for _ in range(len(faults) + 2):  # keep forcing retries until it settles
        rig.clock.advance(hours=2)
        rig.publisher.run_due()

    assert rig.fake.upload_count() == 1, "exactly one video on the channel"
    job = rig.job()
    assert job.status == "succeeded" and job.platform_post_id in rig.fake.videos
    assert next(iter(rig.fake.videos.values()))["size"] == 5120  # and it is complete
    assert rig.fake.faults == []


def test_a_crash_while_uploading_resumes_instead_of_uploading_again(rig, monkeypatch):
    monkeypatch.setattr(youtube, "CHUNK", 1024)
    rig.fake.faults = ["drop_mid_upload"]
    rig.publisher.schedule_clip(1)
    due(rig)
    with rig.factory.begin() as db:  # as if the engine died mid-job
        db.get(PublishJob, 1).status = "running"
    assert rig.job().state["session_url"]  # saved before any bytes were sent

    reborn = Publisher(rig.factory, rig.settings, rig.secrets, rig.publisher.platforms,
                       now=rig.clock, tz=UTC)  # fmt: skip
    reborn.recover()
    assert rig.job().status == "scheduled"
    rig.clock.advance(hours=1)
    reborn.run_due()
    assert rig.fake.upload_count() == 1 and len(rig.fake.sessions) == 1  # same session, resumed
    assert rig.job().status == "succeeded"


def test_two_runs_of_the_same_job_at_once_post_once(rig):
    rig.publisher.schedule_clip(1)
    rig.clock.advance(minutes=11)
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(rig.publisher.run_job(1))) for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert rig.fake.upload_count() == 1 and sorted(results) == ["succeeded"] * 4


def test_duplicate_job_rows_are_impossible(rig):
    rig.publisher.schedule_clip(1)
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError), rig.factory.begin() as db:
        db.add(
            PublishJob(
                clip_id=1, account_id=1, scheduled_at=T0, idempotency_key=idempotency_key(1, 1)
            )
        )


# -- failures -------------------------------------------------------------------------


def test_temporary_failures_back_off_then_give_up(rig):
    rig.fake.faults = ["server_error"] * 20
    rig.publisher.schedule_clip(1)
    waits = []
    for _ in range(MAX_ATTEMPTS):
        rig.clock.advance(hours=3)
        before = rig.clock.now
        rig.publisher.run_due()
        waits.append(rig.job().scheduled_at - before)
    job = rig.job()
    assert job.status == "failed" and "gave up after 5 tries" in job.error
    assert waits[0] < waits[1] < waits[2]  # growing pauses between tries
    assert rig.fake.upload_count() == 0


def test_quota_exhaustion_is_retried_later_not_failed(rig):
    rig.fake.quota_exceeded = True
    rig.publisher.schedule_clip(1)
    due(rig)
    job = rig.job()
    assert job.status == "scheduled" and "quota" in job.error


def test_a_refused_upload_fails_without_retrying(rig):
    rig.fake.reject_metadata = True
    rig.publisher.schedule_clip(1)
    due(rig)
    job = rig.job()
    assert job.status == "failed" and "invalidTitle" in job.error
    assert rig.job().state["attempts"] == 1


def test_expired_sign_in_pauses_the_account_and_alerts(rig):
    rig.fake.refresh_valid = False
    rig.publisher.schedule_clip(1)
    due(rig)
    with rig.factory() as db:
        account = db.get(Account, 1)
        assert account.paused and "Sign-in expired" in account.pause_reason
    assert rig.job().status == "scheduled"  # it will go out once the account is reconnected
    assert len(rig.alerts) == 1 and "sign-in expired" in rig.alerts[0]
    assert REFRESH not in " ".join(rig.alerts)


# -- strikes, removal, metrics --------------------------------------------------------


def test_reporting_a_strike_pauses_the_account_and_alerts(rig):
    assert rig.publisher.register_strike(1, "claim", "Content ID claim on video 3") is True
    with rig.factory() as db:
        account = db.get(Account, 1)
        assert account.paused and account.pause_reason == "claim: Content ID claim on video 3"
    assert rig.alerts == ["My Clips is paused: claim. Content ID claim on video 3"]
    assert rig.audit() == ["account.strike"]
    assert rig.publisher.register_strike(99, "strike") is False


@pytest.mark.parametrize(
    ("damage", "kind"),
    [
        (
            lambda fake: fake.videos["vid001"].update(
                upload_status="rejected", rejection_reason="copyright"
            ),
            "copyright",
        ),
        (lambda fake: fake.videos.clear(), "removed"),
    ],
)
def test_a_rejected_or_removed_post_is_treated_as_a_strike(rig, damage, kind):
    rig.publisher.schedule_clip(1)
    due(rig)
    rig.publisher.check_posts()  # all fine: numbers recorded, no strike
    with rig.factory() as db:
        metric = db.scalars(select(Metric)).one()
        assert (metric.views, metric.likes, metric.comments) == (12, 3, 1)
        assert db.get(Account, 1).paused is False

    damage(rig.fake)
    rig.publisher.check_posts()
    rig.publisher.check_posts()  # a second look must not raise a second strike
    with rig.factory() as db:
        account = db.get(Account, 1)
        assert account.paused and account.pause_reason.startswith(kind)
    assert len(rig.alerts) == 1 and "youtu.be/vid001" in rig.alerts[0]
    assert rig.audit().count("account.strike") == 1


def test_remove_clip_takes_the_video_down(rig):
    rig.publisher.schedule_clip(1)
    due(rig)
    assert rig.publisher.remove_clip(1) is True
    assert rig.fake.videos == {} and rig.job().state["removed"] is True
    assert rig.publisher.remove_clip(1) is True  # already gone counts as removed
    assert rig.publisher.remove_clip(999) is True  # nothing was posted: nothing to remove


def test_lookup_after_a_lost_session_tolerates_clock_differences_but_not_old_videos(rig):
    """The title lookup that prevents a duplicate must survive a few minutes of clock
    difference, and must not adopt an older video that merely has the same title."""
    rig.fake.faults = ["expire_after_complete"]
    rig.publisher.schedule_clip(1)
    due(rig)
    rig.fake.videos["vid001"]["published"] -= timedelta(minutes=8)  # YouTube's clock runs behind
    rig.clock.advance(hours=2)
    rig.publisher.run_due()
    assert rig.fake.upload_count() == 1 and rig.job().platform_post_id == "vid001"

    # A different clip that happens to share a title with a video from last week.
    rig.fake.videos["vid001"]["published"] -= timedelta(days=7)
    second = rig.add_clip(title="He <actually> did it")
    rig.publisher.schedule_clip(second)
    rig.fake.faults = ["drop_mid_upload", "expire_session"]
    for _ in range(3):
        rig.clock.advance(hours=2)
        rig.publisher.run_due()
    assert rig.fake.upload_count() == 2  # uploaded as its own video, not confused with the old one


def test_a_strike_drops_l3_back_to_l2_and_restarts_the_clock(rig):
    rig.settings.update({"autonomy_level": "L3", "autonomy_l2_since": "2026-10-01T00:00:00+00:00"})
    rig.publisher.register_strike(1, "strike", "community guidelines")
    settings = rig.settings.get()
    assert settings.autonomy_level == "L2"
    assert settings.autonomy_l2_since > "2026-11-01"  # the two weeks start again
    assert "Autonomy dropped from L3 to L2" in rig.alerts[0]
    assert rig.audit() == ["account.strike", "autonomy.demote"]

    rig.publisher.register_strike(1, "claim")  # at L2 there is nothing further to drop
    assert rig.settings.get().autonomy_level == "L2" and rig.audit().count("autonomy.demote") == 1
