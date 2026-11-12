"""TikTok (inbox drafts) and Instagram (Reels) publishers against fake servers shaped
like the documented APIs. As with YouTube: a retry must never make a second post."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import keyring
import pytest
from sqlalchemy import select

from engine.db.models import Account, Clip, Moment, PublishJob, Streamer, StreamSession
from engine.db.session import create_db_engine, make_session_factory, run_migrations
from engine.keychain import SecretStore
from engine.logging_setup import SecretRedactor
from engine.publish import tiktok
from engine.publish.instagram import InstagramClient, InstagramPublisher
from engine.publish.service import MAX_WAITS, Publisher
from engine.publish.tiktok import TikTokClient, TikTokPublisher, chunk_plan
from engine.settings import SettingsStore
from tests.fake_keyring import MemoryKeyring

T0 = datetime(2026, 11, 19, 14, 0, tzinfo=UTC)
TT_REF, IG_REF = "acct_tiktok_00000000000000aa", "acct_instagram_00000000000000bb"
MB = 1024 * 1024


class FakeTikTok:
    def __init__(self):
        self.uploads: dict[str, dict] = {}
        self.inits = 0
        self.faults: list[str] = []
        self.processing_polls = 1  # status checks that say "processing" before delivery
        self.fail_processing = False
        self.rotate_to: str | None = None
        self.challenge: str | None = None
        self.token_valid = True

    def drafts(self) -> int:
        return sum(1 for u in self.uploads.values() if u["complete"])

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v2/oauth/token/":
            form = dict(httpx.QueryParams(request.content.decode()))
            assert (form["client_key"], form["client_secret"]) == ("tt-key", "tt-secret")
            if form["grant_type"] == "authorization_code":
                good = hashlib.sha256(form["code_verifier"].encode()).hexdigest() == self.challenge
                if form["code"] != "tt-code" or not good:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                return httpx.Response(200, json={"access_token": "tt-access", "refresh_token": "tt-refresh",
                                                 "open_id": "abcdef123456", "expires_in": 86400})  # fmt: skip
            if not self.token_valid:
                return httpx.Response(400, json={"error": "invalid_grant"})
            body = {
                "access_token": "tt-access",
                "expires_in": 86400,
                "refresh_token": form["refresh_token"],
            }
            if self.rotate_to:
                body["refresh_token"] = self.rotate_to
            return httpx.Response(200, json=body)
        if request.url.host == "open-upload.tiktokapis.com":
            upload = self.uploads[request.url.params["upload_id"]]
            span, total = request.headers["Content-Range"].removeprefix("bytes ").split("/")
            first, last = (int(x) for x in span.split("-"))
            assert int(total) == upload["size"] and len(request.content) == last - first + 1
            upload["received"] = last + 1
            upload["complete"] = upload["received"] == upload["size"]
            if self.faults and self.faults[0] == "drop_after_upload" and upload["complete"]:
                self.faults.pop(0)
                raise httpx.ReadError("connection reset", request=request)
            return httpx.Response(201 if upload["complete"] else 206)
        assert request.headers["Authorization"] == "Bearer tt-access"
        if path == "/v2/user/info/":
            return httpx.Response(403, json={"error": {"code": "scope_not_authorized"}})
        body = json.loads(request.content)
        if path == "/v2/post/publish/inbox/video/init/":
            info = body["source_info"]
            assert info["source"] == "FILE_UPLOAD"
            assert (info["chunk_size"], info["total_chunk_count"]) == chunk_plan(info["video_size"])
            self.inits += 1
            publish_id = f"v_inbox_file~{self.inits}"
            self.uploads[str(self.inits)] = {"publish_id": publish_id, "size": info["video_size"],
                                             "received": 0, "complete": False, "polls": 0}  # fmt: skip
            url = f"https://open-upload.tiktokapis.com/video/?upload_id={self.inits}"
            data = {"publish_id": publish_id, "upload_url": url}
            return httpx.Response(200, json={"data": data, "error": {"code": "ok"}})
        if path == "/v2/post/publish/status/fetch/":
            upload = next(u for u in self.uploads.values() if u["publish_id"] == body["publish_id"])
            status = "PROCESSING_UPLOAD"
            if upload["complete"]:
                upload["polls"] += 1
                if upload["polls"] > self.processing_polls:
                    status = "FAILED" if self.fail_processing else "SEND_TO_USER_INBOX"
            data = {
                "status": status,
                "fail_reason": "file_format_check_failed" if status == "FAILED" else "",
            }
            return httpx.Response(200, json={"data": data, "error": {"code": "ok"}})
        return httpx.Response(404, json={"error": {"code": "not_found"}})


class FakeInstagram:
    def __init__(self, now):
        self.now = now
        self.containers: dict[str, dict] = {}
        self.media: list[dict] = []
        self.faults: list[str] = []
        self.processing_polls = 0
        self.token_valid = True
        self.has_account = True

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host == "rupload.facebook.com":
            assert request.headers["Authorization"] == "OAuth ig-token"
            container = self.containers[path.rsplit("/", 1)[1]]
            assert request.headers["offset"] == "0" and int(request.headers["file_size"]) == len(
                request.content
            )
            container["uploaded"] = True
            return httpx.Response(200, json={"success": True, "message": "Upload successful."})
        if not self.token_valid:
            return httpx.Response(400, json={"error": {"code": 190, "message": "token expired"}})
        assert request.headers["Authorization"] == "Bearer ig-token"
        if path.endswith("/me/accounts"):
            account = {"instagram_business_account": {"id": "1784", "username": "myclips"}}
            return httpx.Response(
                200,
                json={"data": [{"id": "page"}, account] if self.has_account else [{"id": "page"}]},
            )
        if path.endswith("/1784/media") and request.method == "POST":
            body = json.loads(request.content)
            assert body["media_type"] == "REELS" and body["upload_type"] == "resumable"
            container_id = f"c{len(self.containers) + 1}"
            self.containers[container_id] = {"caption": body["caption"], "uploaded": False, "polls": 0,
                                             "published": False, "expired": False}  # fmt: skip
            return httpx.Response(200, json={"id": container_id})
        if path.endswith("/1784/media") and request.method == "GET":
            return httpx.Response(200, json={"data": list(reversed(self.media))})
        if path.endswith("/1784/media_publish"):
            container = self.containers[json.loads(request.content)["creation_id"]]
            assert container["uploaded"] and not container["published"]
            container["published"] = True
            media_id = f"m{len(self.media) + 1}"
            self.media.append({"id": media_id, "caption": container["caption"],
                               "timestamp": self.now().strftime("%Y-%m-%dT%H:%M:%S+0000")})  # fmt: skip
            if self.faults and self.faults[0] == "drop_after_publish":
                self.faults.pop(0)
                raise httpx.ReadError("connection reset", request=request)
            return httpx.Response(200, json={"id": media_id})
        container = self.containers.get(path.rsplit("/", 1)[1])
        if container is not None:
            if container["expired"]:
                status = "EXPIRED"
            elif container["published"]:
                status = "PUBLISHED"
            elif not container["uploaded"]:
                status = "IN_PROGRESS"
            else:
                container["polls"] += 1
                status = "FINISHED" if container["polls"] > self.processing_polls else "IN_PROGRESS"
            return httpx.Response(200, json={"status_code": status, "id": path.rsplit("/", 1)[1]})
        media = next((m for m in self.media if path.endswith("/" + m["id"])), None)
        if media:
            return httpx.Response(200, json={"like_count": 4, "comments_count": 2})
        return httpx.Response(400, json={"error": {"code": 100, "message": "no such object"}})


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


@pytest.fixture
def rig(tmp_path):
    previous = keyring.get_keyring()
    keyring.set_keyring(MemoryKeyring())
    db_engine = create_db_engine(tmp_path / "t.db")
    run_migrations(db_engine)
    factory = make_session_factory(db_engine)
    settings = SettingsStore(tmp_path / "settings.json")
    secrets = SecretStore(SecretRedactor())
    secrets.set(TT_REF, "tt-refresh")
    secrets.set(IG_REF, "ig-token")
    video = tmp_path / "clip_1.mp4"
    video.write_bytes(b"\x07" * 4096)
    with factory.begin() as db:
        db.add(
            Streamer(
                platform="twitch",
                username="shroud",
                display_name="shroud",
                permission_status="granted",
            )
        )
        db.flush()
        db.add(StreamSession(streamer_id=1, started_at=T0))
        db.flush()
        db.add(Moment(session_id=1, peak_ts=T0, status="clipped"))
        db.flush()
        db.add(Clip(moment_id=1, start_ts=T0, end_ts=T0, title="He did it", caption="wow #gta6",
                    status="approved", file_path=str(video), policy_result={"verdict": "clean"}))  # fmt: skip
    clock = Clock()
    fake_tt, fake_ig = FakeTikTok(), FakeInstagram(clock)
    tt_client = TikTokClient(
        lambda: ("tt-key", "tt-secret"), transport=httpx.MockTransport(fake_tt)
    )
    ig_client = InstagramClient(transport=httpx.MockTransport(fake_ig))
    alerts: list[str] = []
    publisher = Publisher(
        factory, settings, secrets,
        {"tiktok": TikTokPublisher(tt_client, clock), "instagram": InstagramPublisher(ig_client, clock)},
        now=clock, uniform=lambda low, high: (low + high) / 2, tz=UTC,
    )  # fmt: skip
    tt_client.on_rotate = lambda old, new: publisher.rotate_token("tiktok", old, new)
    publisher.on_alert(alerts.append)

    def add_account(platform):
        with factory.begin() as db:
            db.add(Account(platform=platform, handle=f"my {platform}",
                           keychain_ref=TT_REF if platform == "tiktok" else IG_REF))  # fmt: skip

    def job(job_id=1):
        with factory() as db:
            return db.get(PublishJob, job_id)

    def run(times=1, minutes=11):
        for _ in range(times):
            clock.now += timedelta(minutes=minutes)
            publisher.run_due()

    yield SimpleNamespace(factory=factory, secrets=secrets, publisher=publisher, tt=fake_tt, ig=fake_ig,
                          clock=clock, add_account=add_account, job=job, run=run, alerts=alerts,
                          tt_client=tt_client, video=video)  # fmt: skip
    tt_client.close()
    ig_client.close()
    db_engine.dispose()
    keyring.set_keyring(previous)


# -- TikTok -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("size", "plan"),
    [
        (3 * MB, (3 * MB, 1)),  # under 5 MB: the whole file as one chunk
        (40 * MB, (40 * MB, 1)),
        (64 * MB, (64 * MB, 1)),
        (64 * MB + 1, (10 * MB, 6)),  # above 64 MB: 10 MB chunks, remainder in the last
        (50_000_123, (50_000_123, 1)),
        (250 * MB + 123, (10 * MB, 25)),
    ],
)
def test_chunk_plan_follows_tiktoks_rules(size, plan):
    assert chunk_plan(size) == plan
    chunk_size, count = plan
    last = size - chunk_size * (count - 1)
    assert 1 <= count <= 1000 and last <= 128 * MB and (count == 1 or chunk_size >= 5 * MB)


def test_tiktok_sign_in_url_uses_hex_pkce_and_the_upload_scope():
    verifier, challenge, state = tiktok.new_pkce()
    url = tiktok.authorization_url("tt-key", "http://127.0.0.1:5050/callback/", challenge, state)
    query = parse_qs(urlsplit(url).query)
    assert url.startswith("https://www.tiktok.com/v2/auth/authorize/?")
    assert query["scope"] == ["video.upload"]  # drafts only: not video.publish
    assert query["code_challenge"] == [hashlib.sha256(verifier.encode()).hexdigest()]
    assert query["redirect_uri"] == ["http://127.0.0.1:5050/callback/"] and verifier not in url


def test_tiktok_delivers_one_draft_to_the_inbox(rig):
    rig.add_account("tiktok")
    rig.publisher.schedule_clip(1)
    rig.run()
    job = rig.job()
    assert job.status == "scheduled" and "processing" in job.error  # waiting, not failing
    assert job.state["attempts"] == 0 and job.state["waits"] == 1
    assert job.scheduled_at == rig.clock.now + timedelta(minutes=2)
    rig.run(minutes=3)
    job = rig.job()
    assert job.status == "succeeded" and job.platform_post_id == "v_inbox_file~1"
    assert rig.tt.drafts() == 1 and rig.tt.inits == 1
    with rig.factory() as db:
        assert db.get(Clip, 1).status == "published"
    assert "TikTok inbox" in rig.publisher.platforms["tiktok"].url("x")


def test_tiktok_retry_after_a_dropped_upload_makes_no_second_draft(rig):
    rig.add_account("tiktok")
    rig.tt.faults = ["drop_after_upload"]  # the bytes arrived, but we never heard back
    rig.publisher.schedule_clip(1)
    rig.run(times=4, minutes=20)
    assert rig.job().status == "succeeded"
    assert rig.tt.inits == 1 and rig.tt.drafts() == 1


def test_tiktok_expired_upload_address_starts_over_without_duplicating(rig):
    rig.add_account("tiktok")
    rig.publisher.schedule_clip(1)
    rig.clock.now += timedelta(minutes=11)
    with rig.factory.begin() as db:  # as if we crashed right after opening the upload
        job = db.get(PublishJob, 1)
        job.state = {"attempts": 1, "publish_id": "v_inbox_file~1", "upload_url": "https://open-upload.tiktokapis.com/video/?upload_id=1",
                     "started_at": (rig.clock.now - timedelta(hours=2)).isoformat(), "uploaded": False}  # fmt: skip
    rig.tt.inits = 1
    rig.tt.uploads["1"] = {
        "publish_id": "v_inbox_file~1",
        "size": 4096,
        "received": 0,
        "complete": False,
        "polls": 0,
    }
    rig.run(times=3, minutes=5)
    assert rig.job().status == "succeeded" and rig.job().platform_post_id == "v_inbox_file~2"
    assert rig.tt.drafts() == 1  # the abandoned upload never became a draft


def test_tiktok_processing_failure_fails_the_job(rig):
    rig.add_account("tiktok")
    rig.tt.fail_processing = True
    rig.publisher.schedule_clip(1)
    rig.run(times=3, minutes=5)
    job = rig.job()
    assert job.status == "failed" and "file_format_check_failed" in job.error


def test_tiktok_waiting_is_bounded(rig):
    rig.add_account("tiktok")
    rig.tt.processing_polls = 10_000
    rig.publisher.schedule_clip(1)
    rig.run(times=MAX_WAITS + 3, minutes=3)
    job = rig.job()
    assert job.status == "failed" and "never finished" in job.error and rig.tt.inits == 1


def test_tiktok_rotated_refresh_token_is_saved_to_the_keychain(rig):
    rig.add_account("tiktok")
    rig.tt.rotate_to = "tt-refresh-NEW"
    rig.publisher.schedule_clip(1)
    rig.run()
    assert rig.secrets.get(TT_REF) == "tt-refresh-NEW"


def test_tiktok_expired_sign_in_pauses_the_account(rig):
    rig.add_account("tiktok")
    rig.tt.token_valid = False
    rig.publisher.schedule_clip(1)
    rig.run()
    with rig.factory() as db:
        assert db.get(Account, 1).paused is True
    assert len(rig.alerts) == 1


def test_tiktok_and_instagram_posts_cannot_be_removed_by_the_app(rig):
    rig.add_account("tiktok")
    rig.publisher.schedule_clip(1)
    rig.run(times=3, minutes=6)
    assert rig.job().status == "succeeded"
    assert rig.publisher.remove_clip(1) is False  # reported, so the owner removes it by hand


# -- Instagram ----------------------------------------------------------------------------


def test_instagram_publishes_one_reel_with_caption_and_credit(rig):
    rig.add_account("instagram")
    rig.publisher.schedule_clip(1)
    rig.run()
    job = rig.job()
    assert job.status == "succeeded" and job.platform_post_id == "m1"
    (media,) = rig.ig.media
    assert media["caption"].startswith("He did it\n\nwow #gta6")
    assert "Clip from shroud: https://www.twitch.tv/shroud" in media["caption"]
    assert len(rig.ig.containers) == 1


def test_instagram_waits_while_the_video_is_processing(rig):
    rig.add_account("instagram")
    rig.ig.processing_polls = 2
    rig.publisher.schedule_clip(1)
    rig.run()
    assert rig.job().status == "scheduled" and rig.job().state["waits"] == 1
    rig.run(times=3, minutes=3)
    assert (
        rig.job().status == "succeeded" and len(rig.ig.media) == 1 and len(rig.ig.containers) == 1
    )


def test_instagram_retry_after_a_lost_publish_reply_adopts_the_reel(rig):
    rig.add_account("instagram")
    rig.ig.faults = ["drop_after_publish"]  # published, but the reply never arrived
    rig.publisher.schedule_clip(1)
    rig.run(times=3, minutes=20)
    assert rig.job().status == "succeeded" and rig.job().platform_post_id == "m1"
    assert len(rig.ig.media) == 1 and len(rig.ig.containers) == 1


def test_instagram_expired_container_starts_a_new_one(rig):
    rig.add_account("instagram")
    rig.ig.processing_polls = 1
    rig.publisher.schedule_clip(1)
    rig.run()
    rig.ig.containers["c1"]["expired"] = True
    rig.run(times=3, minutes=3)
    assert (
        rig.job().status == "succeeded" and len(rig.ig.media) == 1 and len(rig.ig.containers) == 2
    )


def test_instagram_expired_token_pauses_the_account(rig):
    rig.add_account("instagram")
    rig.ig.token_valid = False
    rig.publisher.schedule_clip(1)
    rig.run()
    with rig.factory() as db:
        assert db.get(Account, 1).paused is True


def test_one_clip_goes_to_every_connected_platform_once(rig):
    rig.add_account("tiktok")
    rig.add_account("instagram")
    assert len(rig.publisher.schedule_clip(1)) == 2
    assert rig.publisher.schedule_clip(1) == []
    rig.run(times=4, minutes=30)
    with rig.factory() as db:
        assert [j.status for j in db.scalars(select(PublishJob).order_by(PublishJob.id))] == [
            "succeeded"
        ] * 2
    assert rig.tt.drafts() == 1 and len(rig.ig.media) == 1
