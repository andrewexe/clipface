"""Accounts API and the Google sign-in flow, with a fake Google behind it."""

from __future__ import annotations

import sqlite3
from datetime import UTC
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy import select

from engine.db.models import Account, Clip, Moment, PublishJob, Streamer, StreamSession, utcnow
from engine.publish.service import Publisher
from engine.publish.youtube import YouTubeClient, YouTubePublisher
from tests.fake_youtube import CLIENT, REFRESH, FakeYouTube


@pytest.fixture
def app(harness, tmp_path):
    fake = FakeYouTube()
    client = YouTubeClient(
        lambda: (
            harness.ctx.secrets.get("google_client_id"),
            harness.ctx.secrets.get("google_client_secret"),
        ),
        transport=httpx.MockTransport(fake),
    )
    harness.ctx.publisher = Publisher(
        harness.ctx.session_factory, harness.ctx.settings, harness.ctx.secrets,
        {"youtube": YouTubePublisher(client, utcnow)}, tz=UTC,
    )  # fmt: skip
    harness.ctx.remove_published = harness.ctx.publisher.remove_clip
    harness.ctx.open_browser = False
    harness.fake = fake
    yield harness
    if harness.ctx.oauth_flow is not None:
        harness.ctx.oauth_flow.cancel()
    client.close()


def call(app, method, path, **kwargs):
    return app.client.request(method, path, headers=app.auth, **kwargs)


def set_google_keys(app):
    for name, value in zip(("google_client_id", "google_client_secret"), CLIENT, strict=True):
        assert (
            call(app, "PUT", f"/settings/secrets/{name}", json={"value": value}).status_code == 200
        )


def start_sign_in(app):
    """Begin connecting; returns (redirect_uri, state) as the browser would be given."""
    response = call(app, "POST", "/accounts/youtube/connect")
    assert response.status_code == 200 and response.json()["browser_opened"] is False
    query = parse_qs(urlsplit(response.json()["auth_url"]).query)
    app.fake.challenge = query["code_challenge"][0]  # Google remembers the challenge
    return query["redirect_uri"][0], query["state"][0]


def browser_returns(redirect_uri, **params):
    return httpx.get(redirect_uri, params=params, trust_env=False, timeout=10)


def connect(app):
    set_google_keys(app)
    redirect_uri, state = start_sign_in(app)
    page = browser_returns(redirect_uri, code="good-code", state=state)
    assert page.status_code == 200 and "Account connected" in page.text
    return call(app, "GET", "/accounts").json()


def test_connect_needs_google_keys_first(app):
    assert call(app, "GET", "/accounts").json()["google_keys_set"] is False
    response = call(app, "POST", "/accounts/youtube/connect")
    assert response.status_code == 409 and "Settings" in response.json()["detail"]
    assert app.client.get("/accounts").status_code == 401


def test_sign_in_creates_the_account_and_keeps_the_token_in_the_keychain_only(app):
    body = connect(app)
    (account,) = body["accounts"]
    assert account["handle"] == "My Clips" and account["platform"] == "youtube"
    assert account["paused"] is False and account["daily_cap"] == 3
    assert body["connecting"] is False and body["connect_error"] is None

    stored = {name: value for (_service, name), value in app.keyring.store.items()}
    refs = [name for name in stored if name.startswith("acct_youtube_")]
    assert len(refs) == 1 and stored[refs[0]] == REFRESH
    # Not in any response, the database, or the logs.
    assert REFRESH not in call(app, "GET", "/accounts").text
    app.ctx.db_engine.dispose()
    dump = "\n".join(sqlite3.connect(app.ctx.config.db_path).iterdump())
    assert REFRESH not in dump
    assert "good-code" not in dump
    log_text = app.log_text()
    assert REFRESH not in log_text
    assert "good-code" not in log_text  # the redirect, with its code, is never logged
    assert "at-1" not in log_text
    audit = call(app, "GET", "/audit").json()["entries"]
    assert audit[0]["action"] == "account.connect"


def test_redirect_with_the_wrong_state_is_refused(app):
    set_google_keys(app)
    redirect_uri, state = start_sign_in(app)
    forged = browser_returns(redirect_uri, code="good-code", state="not-the-state")
    assert forged.status_code == 400
    assert call(app, "GET", "/accounts").json()["accounts"] == []
    assert (
        call(app, "GET", "/accounts").json()["connecting"] is True
    )  # still waiting for the real one
    assert browser_returns(redirect_uri + "/other/path", code="x", state=state).status_code == 404
    real = browser_returns(redirect_uri, code="good-code", state=state)
    assert "Account connected" in real.text
    try:  # one use only: either refused, or the listener has already shut down
        replay = browser_returns(redirect_uri, code="good-code", state=state)
        assert replay.status_code == 400
    except httpx.ConnectError:
        pass


def test_listener_is_loopback_only(app):
    set_google_keys(app)
    redirect_uri, _state = start_sign_in(app)
    assert urlsplit(redirect_uri).hostname == "127.0.0.1"


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"error": "access_denied"}, "cancelled or refused"),
        ({"code": "stolen-or-wrong-code"}, "rejected the sign-in"),
    ],
)
def test_failed_sign_in_is_reported_and_creates_nothing(app, params, message):
    set_google_keys(app)
    redirect_uri, state = start_sign_in(app)
    browser_returns(redirect_uri, state=state, **params)
    body = call(app, "GET", "/accounts").json()
    assert body["accounts"] == [] and message in body["connect_error"]


def test_reconnecting_reuses_the_account_and_clears_an_expired_sign_in(app):
    connect(app)
    with app.ctx.session_factory.begin() as db:
        account = db.get(Account, 1)
        account.paused, account.pause_reason = (
            True,
            "Sign-in expired. Disconnect and connect again.",
        )
    redirect_uri, state = start_sign_in(app)
    browser_returns(redirect_uri, code="good-code", state=state)
    (account,) = call(app, "GET", "/accounts").json()["accounts"]
    assert account["id"] == 1 and account["paused"] is False


def test_cap_pause_resume_and_strike(app):
    connect(app)
    assert call(app, "PATCH", "/accounts/1", json={"daily_cap": 5}).json()["daily_cap"] == 5
    assert call(app, "PATCH", "/accounts/1", json={"daily_cap": 500}).status_code == 422
    paused = call(app, "PATCH", "/accounts/1", json={"paused": True}).json()
    assert paused["paused"] is True and paused["pause_reason"] == "Paused by you."
    assert call(app, "PATCH", "/accounts/1", json={"paused": False}).json()["pause_reason"] is None

    struck = call(
        app,
        "POST",
        "/accounts/1/strike",
        json={"kind": "takedown", "note": "email from the streamer's manager"},
    )
    assert struck.status_code == 200 and struck.json()["paused"] is True
    assert struck.json()["pause_reason"] == "takedown: email from the streamer's manager"
    assert call(app, "POST", "/accounts/1/strike", json={"kind": "whatever"}).status_code == 422
    assert call(app, "POST", "/accounts/99/strike", json={"kind": "claim"}).status_code == 404
    assert call(app, "PATCH", "/accounts/99", json={}).status_code == 404
    actions = [e["action"] for e in call(app, "GET", "/audit").json()["entries"]]
    assert {"account.daily_cap", "account.pause", "account.resume", "account.strike"} <= set(
        actions
    )


def seed_clip(app, permission="granted", tmp_name="clip_1.mp4"):
    app.ctx.config.clips_dir.mkdir(parents=True, exist_ok=True)
    video = app.ctx.config.clips_dir / tmp_name
    video.write_bytes(b"\x00" * 2048)
    with app.ctx.session_factory.begin() as db:
        db.add(
            Streamer(
                platform="twitch",
                username="shroud",
                display_name="shroud",
                permission_status=permission,
            )
        )
        db.flush()
        db.add(StreamSession(streamer_id=1, started_at=utcnow()))
        db.flush()
        db.add(Moment(session_id=1, peak_ts=utcnow(), status="clipped"))
        db.flush()
        db.add(Clip(moment_id=1, start_ts=utcnow(), end_ts=utcnow(), title="A clip", status="pending",
                    file_path=str(video), policy_result={"verdict": "clean"}))  # fmt: skip
    call(
        app, "PATCH", "/settings/app", json={"export_dir": str(app.ctx.config.data_dir / "export")}
    )


def test_approving_a_clip_schedules_its_post_and_jobs_can_be_cancelled_and_retried(app):
    connect(app)
    seed_clip(app)
    approved = call(app, "POST", "/clips/1/approve").json()
    assert approved["status"] == "approved" and approved["scheduled_posts"] == 1

    (job,) = call(app, "GET", "/publish/jobs").json()["jobs"]
    assert (job["status"], job["account"], job["title"], job["url"]) == (
        "scheduled",
        "My Clips",
        "A clip",
        None,
    )
    assert call(app, "POST", "/publish/jobs/1/cancel").json()["status"] == "cancelled"
    assert call(app, "POST", "/publish/jobs/1/cancel").status_code == 409
    assert call(app, "POST", "/publish/jobs/1/retry").json()["status"] == "scheduled"
    assert call(app, "POST", "/publish/jobs/99/retry").status_code == 404

    app.ctx.publisher.run_due()
    (job,) = call(app, "GET", "/publish/jobs").json()["jobs"]
    assert job["status"] == "succeeded" and job["url"] == "https://youtu.be/vid001"
    assert call(app, "GET", "/clips/1").json()["status"] == "published"


def test_no_permission_means_export_only(app):
    connect(app)
    seed_clip(app, permission="none")
    approved = call(app, "POST", "/clips/1/approve").json()
    assert approved["scheduled_posts"] == 0 and approved["meta"]["exported_path"]
    assert call(app, "GET", "/publish/jobs").json()["jobs"] == []


def test_stop_request_takes_the_video_down_from_youtube(app):
    connect(app)
    seed_clip(app)
    call(app, "POST", "/clips/1/approve")
    with app.ctx.session_factory.begin() as db:
        db.get(PublishJob, 1).scheduled_at = utcnow()
    app.ctx.publisher.run_due()
    assert app.fake.upload_count() == 1

    result = call(app, "POST", "/streamers/1/stop").json()
    assert result["removed"] == 1 and result["could_not_remove"] == []
    assert app.fake.videos == {}
    assert call(app, "GET", "/clips/1").json()["status"] == "removed"


def test_disconnect_deletes_the_token_revokes_it_and_drops_waiting_posts(app):
    connect(app)
    seed_clip(app)
    call(app, "POST", "/clips/1/approve")
    assert call(app, "DELETE", "/accounts/1").status_code == 200
    assert call(app, "GET", "/accounts").json()["accounts"] == []
    assert not [name for (_s, name) in app.keyring.store if name.startswith("acct_")]
    assert app.fake.revoked == [REFRESH]
    with app.ctx.session_factory() as db:
        assert db.scalars(select(PublishJob)).all() == []
    assert call(app, "DELETE", "/accounts/1").status_code == 404
