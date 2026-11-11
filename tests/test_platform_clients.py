"""Twitch and Kick clients against fake HTTP servers shaped like the documented APIs."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from engine.monitor.platforms import (
    KickClient,
    MissingCredentials,
    PlatformAuthError,
    PlatformError,
    TwitchClient,
)

pytestmark = pytest.mark.anyio

CREDS = ("client-id-123", "client-secret-456")


class FakeTwitch:
    def __init__(self):
        self.live = {}  # login -> stream row
        self.users = {"shroud": "shroud", "pokimane": "Pokimane"}
        self.valid_secret = CREDS[1]
        self.tokens_issued = 0
        self.expire_next = False
        self.calls = []
        self.status_override = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if request.url.host == "id.twitch.tv":
            assert request.method == "POST" and request.url.path == "/oauth2/token"
            form = dict(httpx.QueryParams(request.content.decode()))
            assert form["grant_type"] == "client_credentials"
            if form["client_secret"] != self.valid_secret:
                return httpx.Response(403, json={"status": 403, "message": "invalid client secret"})
            self.tokens_issued += 1
            return httpx.Response(
                200,
                json={
                    "access_token": f"tok{self.tokens_issued}",
                    "expires_in": 5011271,
                    "token_type": "bearer",
                },
            )
        assert request.url.host == "api.twitch.tv"
        assert request.headers["Client-Id"] == CREDS[0]
        if self.status_override:
            return self.status_override
        if self.expire_next:
            self.expire_next = False
            return httpx.Response(401, json={"status": 401, "message": "Invalid OAuth token"})
        assert request.headers["Authorization"] == f"Bearer tok{self.tokens_issued}"
        if request.url.path == "/helix/streams":
            logins = request.url.params.get_list("user_login")
            assert 1 <= len(logins) <= 100
            return httpx.Response(
                200, json={"data": [self.live[n] for n in logins if n in self.live]}
            )
        if request.url.path == "/helix/users":
            logins = request.url.params.get_list("login")
            assert 1 <= len(logins) <= 100
            data = [
                {"id": "1", "login": n, "display_name": self.users[n]}
                for n in logins
                if n in self.users
            ]
            return httpx.Response(200, json={"data": data})
        return httpx.Response(404)


def twitch_row(login, started="2026-11-11T18:00:00Z"):
    return {
        "id": "40952121085",
        "user_id": "101",
        "user_login": login,
        "user_name": login.title(),
        "game_id": "1",
        "game_name": "Grand Theft Auto VI",
        "type": "live",
        "title": "day one",
        "viewer_count": 78365,
        "started_at": started,
    }


@pytest.fixture
def twitch():
    server = FakeTwitch()
    client = TwitchClient(lambda: CREDS, transport=httpx.MockTransport(server))
    return server, client


async def test_twitch_live_and_offline(twitch):
    server, client = twitch
    server.live["shroud"] = twitch_row("shroud")
    snap = await client.check(["shroud", "pokimane"], verify=set())
    assert set(snap.live) == {"shroud"}
    info = snap.live["shroud"]
    assert info.started_at == datetime(2026, 11, 11, 18, 0, tzinfo=UTC)
    assert (info.title, info.game, info.viewer_count) == ("day one", "Grand Theft Auto VI", 78365)
    assert info.stream_id == "40952121085"


async def test_twitch_verify_finds_and_misses(twitch):
    server, client = twitch
    snap = await client.check(
        ["shroud", "pokimane", "nobody_here"], verify={"pokimane", "nobody_here"}
    )
    assert snap.found == {"pokimane": "Pokimane"}
    assert snap.missing == {"nobody_here"}


async def test_twitch_token_is_reused_and_refreshed_on_401(twitch):
    server, client = twitch
    await client.check(["shroud"], verify=set())
    await client.check(["shroud"], verify=set())
    assert server.tokens_issued == 1
    server.expire_next = True
    server.live["shroud"] = twitch_row("shroud")
    snap = await client.check(["shroud"], verify=set())
    assert server.tokens_issued == 2 and "shroud" in snap.live


async def test_twitch_batches_over_100(twitch):
    server, client = twitch
    names = [f"user{i:03d}" for i in range(230)]
    await client.check(names, verify=set())
    stream_calls = [c for c in server.calls if c.url.path == "/helix/streams"]
    assert [len(c.url.params.get_list("user_login")) for c in stream_calls] == [100, 100, 30]


async def test_bad_secret_is_auth_error_and_not_retried_every_poll(twitch):
    server, _ = twitch
    client = TwitchClient(lambda: (CREDS[0], "wrong"), transport=httpx.MockTransport(server))
    with pytest.raises(PlatformAuthError):
        await client.check(["shroud"], verify=set())
    with pytest.raises(PlatformAuthError):
        await client.check(["shroud"], verify=set())
    token_calls = [c for c in server.calls if c.url.host == "id.twitch.tv"]
    assert len(token_calls) == 1


async def test_new_credentials_are_tried_immediately(twitch):
    server, _ = twitch
    current = [(CREDS[0], "wrong")]
    client = TwitchClient(lambda: current[0], transport=httpx.MockTransport(server))
    with pytest.raises(PlatformAuthError):
        await client.check(["shroud"], verify=set())
    current[0] = CREDS  # the user fixed the key in Settings
    await client.check(["shroud"], verify=set())


async def test_missing_credentials_makes_no_request(twitch):
    server, _ = twitch
    for creds in (None, ("", ""), ("id-only", "")):
        client = TwitchClient(lambda c=creds: c, transport=httpx.MockTransport(server))
        with pytest.raises(MissingCredentials):
            await client.check(["shroud"], verify=set())
    assert server.calls == []


async def test_rate_limit_and_server_errors_are_temporary(twitch):
    server, client = twitch
    server.status_override = httpx.Response(429, headers={"Ratelimit-Reset": "0"})
    with pytest.raises(PlatformError) as excinfo:
        await client.check(["shroud"], verify=set())
    assert not isinstance(excinfo.value, PlatformAuthError)
    assert excinfo.value.retry_after_s and excinfo.value.retry_after_s >= 1
    server.status_override = httpx.Response(503)
    with pytest.raises(PlatformError):
        await client.check(["shroud"], verify=set())
    server.status_override = httpx.Response(200, text="<html>not json</html>")
    with pytest.raises(PlatformError):
        await client.check(["shroud"], verify=set())


async def test_network_failure_is_temporary_and_hides_details():
    def boom(request):
        raise httpx.ConnectError(f"cannot reach {request.url}")

    client = TwitchClient(lambda: CREDS, transport=httpx.MockTransport(boom))
    with pytest.raises(PlatformError) as excinfo:
        await client.check(["shroud"], verify=set())
    assert CREDS[1] not in str(excinfo.value)


async def test_malformed_rows_are_ignored(twitch):
    server, client = twitch
    server.live["shroud"] = {"user_login": "shroud", "type": "live", "started_at": "garbage"}
    server.live["pokimane"] = "not-an-object"
    snap = await client.check(["shroud", "pokimane"], verify=set())
    assert set(snap.live) == {"shroud"}
    assert snap.live["shroud"].started_at is None and snap.live["shroud"].title is None


class FakeKick:
    def __init__(self):
        self.channels = {}
        self.calls = []

    def channel(self, slug, live, user_id=7, started="2026-11-11T18:00:00Z"):
        self.channels[slug] = {
            "broadcaster_user_id": user_id,
            "slug": slug,
            "channel_description": "",
            "banner_picture": "",
            "stream": {
                "is_live": live,
                "is_mature": False,
                "key": "",
                "language": "en",
                "start_time": started if live else "0001-01-01T00:00:00Z",
                "thumbnail": "",
                "url": "",
                "viewer_count": 4200 if live else 0,
            },
            "stream_title": "kick day one" if live else "",
            "category": {"id": 15, "name": "GTA VI", "thumbnail": ""},
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if request.url.host == "id.kick.com":
            assert request.url.path == "/oauth/token"
            assert request.headers["content-type"] == "application/x-www-form-urlencoded"
            form = dict(httpx.QueryParams(request.content.decode()))
            if form["client_secret"] != CREDS[1]:
                return httpx.Response(400, json={"error": "Invalid request"})
            return httpx.Response(
                200, json={"access_token": "ktok", "expires_in": 3600, "token_type": "Bearer"}
            )
        assert request.url.host == "api.kick.com"
        assert request.headers["Authorization"] == "Bearer ktok"
        if request.url.path == "/public/v1/channels":
            slugs = request.url.params.get_list("slug")
            assert 1 <= len(slugs) <= 50 and all(len(s) <= 25 for s in slugs)
            data = [self.channels[s] for s in slugs if s in self.channels]
            return httpx.Response(200, json={"data": data, "message": "OK"})
        if request.url.path == "/public/v1/users":
            ids = request.url.params.get_list("id")
            data = [{"user_id": int(i), "name": "xQc", "profile_picture": ""} for i in ids]
            return httpx.Response(200, json={"data": data, "message": "OK"})
        return httpx.Response(404)


@pytest.fixture
def kick():
    server = FakeKick()
    return server, KickClient(lambda: CREDS, transport=httpx.MockTransport(server))


async def test_kick_live_offline_and_missing(kick):
    server, client = kick
    server.channel("xqc", live=True)
    server.channel("sleepy", live=False, user_id=8)
    snap = await client.check(["xqc", "sleepy", "ghost"], verify={"xqc", "sleepy", "ghost"})
    assert set(snap.live) == {"xqc"}
    info = snap.live["xqc"]
    assert info.started_at == datetime(2026, 11, 11, 18, 0, tzinfo=UTC)
    assert (info.title, info.game, info.viewer_count) == ("kick day one", "GTA VI", 4200)
    assert snap.missing == {"ghost"}
    assert snap.found["xqc"] == "xQc" and "sleepy" in snap.found


async def test_kick_batches_over_50(kick):
    server, client = kick
    await client.check([f"chan{i:03d}" for i in range(120)], verify=set())
    calls = [c for c in server.calls if c.url.path == "/public/v1/channels"]
    assert [len(c.url.params.get_list("slug")) for c in calls] == [50, 50, 20]


async def test_kick_bad_secret(kick):
    server, _ = kick
    client = KickClient(lambda: (CREDS[0], "nope"), transport=httpx.MockTransport(server))
    with pytest.raises(PlatformAuthError):
        await client.check(["xqc"], verify=set())
