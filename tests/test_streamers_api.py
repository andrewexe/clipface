"""Streamers API: add by URL, list, pause, set permission, remove."""

from sqlalchemy import select

from engine.db.models import Streamer, StreamSession, utcnow


def add(harness, url):
    return harness.client.post("/streamers", json={"url": url}, headers=harness.auth)


def listing(harness):
    return harness.client.get("/streamers", headers=harness.auth).json()["streamers"]


def audit(harness):
    entries = harness.client.get("/audit", headers=harness.auth).json()["entries"]
    return [e["action"] for e in reversed(entries)]


def test_requires_token(harness):
    assert harness.client.get("/streamers").status_code == 401
    assert harness.client.post("/streamers", json={"url": "twitch.tv/x"}).status_code == 401


def test_add_parses_url_and_lists(harness):
    response = add(harness, "https://m.twitch.tv/Shroud/videos?x=1")
    assert response.status_code == 201
    body = response.json()
    assert (body["platform"], body["username"]) == ("twitch", "shroud")
    assert body["url"] == "https://www.twitch.tv/shroud"
    # Safe defaults: watched, but never auto-published without permission.
    assert body["enabled"] is True and body["permission_status"] == "none"
    assert body["state"] == "checking" and body["session"] is None

    add(harness, "kick.com/xqc")
    assert [(s["platform"], s["username"]) for s in listing(harness)] == [
        ("twitch", "shroud"),
        ("kick", "xqc"),
    ]
    assert audit(harness) == ["streamer.add", "streamer.add"]


def test_bad_urls_get_a_readable_422(harness):
    for url in ["", "shroud", "https://youtube.com/@x", "https://twitch.tv/videos/123"]:
        response = add(harness, url)
        assert response.status_code == 422, url
        assert isinstance(response.json()["detail"], str) and response.json()["detail"]
    assert add(harness, "x" * 5000).status_code == 422
    assert listing(harness) == []


def test_duplicates_rejected_across_url_forms(harness):
    assert add(harness, "https://www.twitch.tv/shroud").status_code == 201
    response = add(harness, "twitch.tv/SHROUD/clips")
    assert response.status_code == 409
    assert len(listing(harness)) == 1
    # Same name on the other platform is a different channel.
    assert add(harness, "https://kick.com/shroud").status_code == 201


def test_live_state_comes_from_open_session(harness):
    streamer_id = add(harness, "twitch.tv/shroud").json()["id"]
    with harness.ctx.session_factory.begin() as db:
        db.add(StreamSession(streamer_id=streamer_id, started_at=utcnow(), title="t", game="g"))
    (item,) = listing(harness)
    assert item["state"] == "live"
    assert (item["session"]["title"], item["session"]["game"]) == ("t", "g")


def test_pause_closes_live_session_and_resume(harness):
    streamer_id = add(harness, "twitch.tv/shroud").json()["id"]
    with harness.ctx.session_factory.begin() as db:
        db.add(StreamSession(streamer_id=streamer_id, started_at=utcnow()))
    response = harness.client.patch(
        f"/streamers/{streamer_id}", json={"enabled": False}, headers=harness.auth
    )
    assert response.status_code == 200
    assert response.json()["state"] == "paused" and response.json()["session"] is None
    with harness.ctx.session_factory() as db:
        assert db.scalars(select(StreamSession)).one().ended_at is not None

    again = harness.client.patch(
        f"/streamers/{streamer_id}", json={"enabled": True}, headers=harness.auth
    )
    assert again.json()["enabled"] is True
    assert audit(harness) == ["streamer.add", "streamer.disable", "session.end", "streamer.enable"]


def test_permission_status_is_validated_and_audited(harness):
    streamer_id = add(harness, "twitch.tv/shroud").json()["id"]
    bad = harness.client.patch(
        f"/streamers/{streamer_id}", json={"permission_status": "sure"}, headers=harness.auth
    )
    assert bad.status_code == 422
    ok = harness.client.patch(
        f"/streamers/{streamer_id}", json={"permission_status": "granted"}, headers=harness.auth
    )
    assert ok.json()["permission_status"] == "granted"
    entries = harness.client.get("/audit", headers=harness.auth).json()["entries"]
    assert entries[0]["action"] == "streamer.permission"
    assert entries[0]["details"] == {"from": "none", "to": "granted"}
    # No change, no audit noise.
    harness.client.patch(
        f"/streamers/{streamer_id}", json={"permission_status": "granted"}, headers=harness.auth
    )
    assert audit(harness).count("streamer.permission") == 1


def test_remove(harness):
    streamer_id = add(harness, "twitch.tv/shroud").json()["id"]
    assert (
        harness.client.delete(f"/streamers/{streamer_id}", headers=harness.auth).status_code == 200
    )
    assert listing(harness) == []
    assert (
        harness.client.delete(f"/streamers/{streamer_id}", headers=harness.auth).status_code == 404
    )
    assert (
        harness.client.patch(f"/streamers/{streamer_id}", json={}, headers=harness.auth).status_code
        == 404
    )
    with harness.ctx.session_factory() as db:
        assert db.scalars(select(Streamer)).all() == []
    assert audit(harness)[-1] == "streamer.remove"


def test_facecam_layout_is_validated_saved_and_audited(harness):
    streamer_id = add(harness, "twitch.tv/shroud").json()["id"]
    assert listing(harness)[0]["facecam"] is None

    def patch(layout):
        return harness.client.patch(
            f"/streamers/{streamer_id}", json={"layout": layout}, headers=harness.auth
        )

    for bad in ({"facecam": {"x": 0.5}}, {"facecam": {"x": 2, "y": 0, "w": 0.5, "h": 0.5}},
                {"facecam": {"x": 0, "y": 0, "w": 0.01, "h": 0.5}}):  # fmt: skip
        assert patch(bad).status_code == 422, bad
    ok = patch({"facecam": {"x": 0.7, "y": 0.6, "w": 0.3, "h": 0.4}})
    assert ok.status_code == 200
    assert ok.json()["facecam"] == {"x": 0.7, "y": 0.6, "w": 0.3, "h": 0.4}
    assert patch({"facecam": {"x": 0.7, "y": 0.6, "w": 0.3, "h": 0.4}}).status_code == 200
    assert patch({"facecam": None}).json()["facecam"] is None
    assert audit(harness).count("streamer.layout") == 2  # set and cleared; the no-op is silent


def test_frame_needs_footage(harness):
    streamer_id = add(harness, "twitch.tv/shroud").json()["id"]
    response = harness.client.get(f"/streamers/{streamer_id}/frame", headers=harness.auth)
    assert response.status_code == 404 and "No recent footage" in response.json()["detail"]
    assert harness.client.get("/streamers/999/frame", headers=harness.auth).status_code == 404
