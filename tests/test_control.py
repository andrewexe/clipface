"""Safety controls: kill switch, autonomy levels, stop requests, program rules API."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from engine.db.audit import write_audit
from engine.db.models import (
    Account,
    AuditLog,
    Clip,
    Moment,
    PublishJob,
    Streamer,
    StreamSession,
    utcnow,
)


def call(harness, method, path, **kwargs):
    return harness.client.request(method, path, headers=harness.auth, **kwargs)


def audit(harness):
    entries = call(harness, "GET", "/audit").json()["entries"]
    return [e["action"] for e in reversed(entries)]


def test_defaults_are_the_safe_ones(harness):
    body = call(harness, "GET", "/control").json()
    assert body["autonomy_level"] == "L1" and body["publishing_paused"] is False
    assert body["l3"]["eligible"] is False
    assert harness.client.get("/control").status_code == 401


def test_kill_switch_pauses_and_resumes(harness):
    on = call(harness, "POST", "/control/kill-switch", json={"paused": True})
    assert on.status_code == 200 and on.json()["publishing_paused"] is True
    assert harness.ctx.settings.get().publishing_paused is True
    call(harness, "POST", "/control/kill-switch", json={"paused": True})  # again: no second entry
    off = call(harness, "POST", "/control/kill-switch", json={"paused": False})
    assert off.json()["publishing_paused"] is False
    assert audit(harness) == ["publishing.pause", "publishing.resume"]


def test_autonomy_l1_l2_and_l3_stays_locked(harness):
    l2 = call(harness, "POST", "/control/autonomy", json={"level": "L2"}).json()
    assert l2["autonomy_level"] == "L2" and l2["autonomy_l2_since"]
    assert "more day" in l2["l3"]["reason"]

    refused = call(harness, "POST", "/control/autonomy", json={"level": "L3"})
    assert refused.status_code == 409 and "not available yet" in refused.json()["detail"]
    assert call(harness, "POST", "/control/autonomy", json={"level": "L9"}).status_code == 422

    # Two weeks in, with a strike since: still locked, and it says why.
    then = (utcnow() - timedelta(days=15)).isoformat()
    harness.ctx.settings.update({"autonomy_l2_since": then})
    with harness.ctx.session_factory.begin() as db:
        write_audit(db, actor="system", action="account.strike", object_id="account:1")
    assert "strike" in call(harness, "GET", "/control").json()["l3"]["reason"]

    # Two clean weeks: eligible, but L3 is still refused because the plan never defines it.
    with harness.ctx.session_factory.begin() as db:
        for entry in db.scalars(select(AuditLog).where(AuditLog.action == "account.strike")):
            db.delete(entry)
    assert call(harness, "GET", "/control").json()["l3"]["eligible"] is True
    still = call(harness, "POST", "/control/autonomy", json={"level": "L3"})
    assert still.status_code == 409 and "not built" in still.json()["detail"]
    assert harness.ctx.settings.get().autonomy_level == "L2"

    l1 = call(harness, "POST", "/control/autonomy", json={"level": "L1"}).json()
    assert l1["autonomy_level"] == "L1" and l1["autonomy_l2_since"] is None
    assert audit(harness).count("autonomy.set") == 2


def seed_streamer_with_clips(harness):
    with harness.ctx.session_factory.begin() as db:
        db.add(Streamer(platform="twitch", username="shroud", permission_status="granted"))
        db.add(Streamer(platform="kick", username="other", permission_status="granted"))
        db.add(Account(platform="youtube", handle="@clips"))
        db.flush()
        db.add(StreamSession(streamer_id=1, started_at=utcnow()))  # live right now
        db.add(StreamSession(streamer_id=2, started_at=utcnow()))
        db.flush()
        for session_id, status in ((1, "pending"), (1, "approved"), (1, "published"),
                                   (1, "published"), (1, "rejected"), (2, "pending")):  # fmt: skip
            moment = Moment(session_id=session_id, peak_ts=utcnow(), status="clipped")
            db.add(moment)
            db.flush()
            db.add(Clip(moment_id=moment.id, start_ts=utcnow(), end_ts=utcnow(), status=status))
        db.flush()
        db.add(PublishJob(clip_id=2, account_id=1, scheduled_at=utcnow(), idempotency_key="k1"))
        db.add(PublishJob(clip_id=3, account_id=1, scheduled_at=utcnow(), idempotency_key="k2",
                          status="succeeded", platform_post_id="yt123"))  # fmt: skip


def statuses(harness):
    with harness.ctx.session_factory() as db:
        return [c.status for c in db.scalars(select(Clip).order_by(Clip.id))]


def test_stop_request_is_one_action(harness):
    seed_streamer_with_clips(harness)
    removed = []
    harness.ctx.remove_published = lambda clip_id: removed.append(clip_id) or clip_id == 3

    response = call(harness, "POST", "/streamers/1/stop")

    body = response.json()
    assert response.status_code == 200
    assert body["streamer"]["enabled"] is False and body["streamer"]["permission_status"] == "none"
    assert body["streamer"]["state"] == "paused"  # their live session was closed
    assert (body["dropped"], body["removed"], body["cancelled_posts"]) == (2, 1, 1)
    assert body["could_not_remove"] == [4]  # reported, so it can be taken down by hand
    assert removed == [3, 4]
    # Their clips: pending and approved dropped, one post removed, one still up (reported).
    assert statuses(harness) == [
        "rejected",
        "rejected",
        "removed",
        "published",
        "rejected",
        "pending",
    ]
    with harness.ctx.session_factory() as db:
        assert db.get(PublishJob, 1).status == "cancelled"
        assert db.get(PublishJob, 2).status == "succeeded"
        assert db.get(StreamSession, 1).ended_at is not None
        assert db.get(StreamSession, 2).ended_at is None  # the other streamer is untouched
    assert "streamer.stop" in audit(harness)


def test_stop_without_a_publisher_reports_what_is_still_up(harness):
    seed_streamer_with_clips(harness)
    body = call(harness, "POST", "/streamers/1/stop").json()
    assert body["removed"] == 0 and body["could_not_remove"] == [3, 4]
    assert call(harness, "POST", "/streamers/99/stop").status_code == 404


def test_program_rules_are_saved_cleaned_and_used_in_the_description(harness, tmp_path):
    seed_streamer_with_clips(harness)
    rules = {"required_tags": ["ShroudClips", "bad tag"], "credit_format": "Watch {name}: {url}",
             "banned": ["rival game"], "junk": 1}  # fmt: skip
    saved = call(harness, "PATCH", "/streamers/1", json={"program_rules": rules}).json()
    assert saved["program_rules"] == {
        "required_tags": ["#ShroudClips"],
        "credit_format": "Watch {name}: {url}",
        "banned": ["rival game"],
    }
    call(harness, "PATCH", "/streamers/1", json={"program_rules": rules})  # same again
    assert audit(harness).count("streamer.program_rules") == 1

    with harness.ctx.session_factory.begin() as db:
        db.get(Clip, 1).caption = "What a play #gta6"
    clip = call(harness, "GET", "/clips/1").json()
    assert clip["credit"] == "Watch shroud: https://www.twitch.tv/shroud"
    assert clip["description"] == (
        "What a play #gta6\n\n#ShroudClips\n\nWatch shroud: https://www.twitch.tv/shroud"
    )
