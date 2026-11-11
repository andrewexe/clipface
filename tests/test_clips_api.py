"""Review queue API: list, preview, edit, approve (exports), reject, restore."""

from __future__ import annotations

import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

from engine.db.models import Clip, Moment, Streamer, StreamSession, utcnow
from engine.media.ffmpeg import ffmpeg_path
from engine.publish.export import safe_name


@pytest.fixture(scope="module")
def tiny_video(tmp_path_factory):
    path = tmp_path_factory.mktemp("tiny") / "tiny.mp4"
    subprocess.run(
        [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "testsrc2=size=108x192:rate=10", "-t", "1", "-pix_fmt", "yuv420p", str(path)],
        check=True,
    )  # fmt: skip
    return path


@pytest.fixture
def queue(harness, tiny_video, tmp_path):
    """Three pending clips for one streamer, with real video files."""
    export_dir = tmp_path / "export"
    harness.client.patch(
        "/settings/app", json={"export_dir": str(export_dir)}, headers=harness.auth
    )
    harness.ctx.config.clips_dir.mkdir(parents=True, exist_ok=True)
    with harness.ctx.session_factory.begin() as db:
        db.add(Streamer(platform="twitch", username="shroud", display_name="shroud"))
        db.flush()
        db.add(StreamSession(streamer_id=1, started_at=utcnow()))
        db.flush()
        for i in range(3):
            moment = Moment(session_id=1, peak_ts=utcnow(), status="clipped",
                            signal_scores={"triggers": ["chat"], "chat": {"z": 5.0}})  # fmt: skip
            db.add(moment)
            db.flush()
            clip = Clip(moment_id=moment.id, start_ts=utcnow(), end_ts=utcnow() + timedelta(seconds=40),
                        title=f"Moment {i + 1}", status="pending", transcript="no way",
                        meta={"duration": 40.0, "captions": True})  # fmt: skip
            db.add(clip)
            db.flush()
            video = harness.ctx.config.clips_dir / f"clip_{clip.id}.mp4"
            video.write_bytes(tiny_video.read_bytes())
            video.with_suffix(".jpg").write_bytes(b"\xff\xd8\xff\xe0fakejpeg")
            clip.file_path = str(video)
    return type("Queue", (), dict(h=harness, export_dir=export_dir))


def call(queue, method, path, **kwargs):
    return queue.h.client.request(method, path, headers=queue.h.auth, **kwargs)


def audit(queue):
    entries = call(queue, "GET", "/audit").json()["entries"]
    return [e["action"] for e in reversed(entries)]


def test_requires_token(queue):
    for path in ("/clips", "/clips/1", "/clips/1/video", "/clips/1/thumbnail"):
        assert queue.h.client.get(path).status_code == 401
    assert queue.h.client.post("/clips/1/approve").status_code == 401


def test_list_pending_oldest_first_with_counts(queue):
    body = call(queue, "GET", "/clips").json()
    assert [c["title"] for c in body["clips"]] == ["Moment 1", "Moment 2", "Moment 3"]
    assert body["counts"] == {"pending": 3}
    assert body["export_dir"] == str(queue.export_dir)
    clip = body["clips"][0]
    assert clip["streamer"]["name"] == "shroud" and clip["triggers"] == ["chat"]
    assert clip["credit"] == "Clip from shroud: https://www.twitch.tv/shroud"
    assert clip["has_video"] is True and clip["duration"] == 40.0
    assert call(queue, "GET", "/clips", params={"status": "approved"}).json()["clips"] == []
    assert call(queue, "GET", "/clips", params={"status": "nope"}).status_code == 422


def test_video_and_thumbnail_are_served(queue, tiny_video):
    video = call(queue, "GET", "/clips/1/video")
    assert video.status_code == 200 and video.headers["content-type"] == "video/mp4"
    assert video.content == tiny_video.read_bytes()
    thumb = call(queue, "GET", "/clips/1/thumbnail")
    assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/jpeg"
    assert call(queue, "GET", "/clips/99/video").status_code == 404


def test_only_files_in_the_clips_folder_are_served(queue, tmp_path):
    secret = tmp_path / "private.mp4"
    secret.write_bytes(b"not yours")
    with queue.h.ctx.session_factory.begin() as db:
        db.get(Clip, 1).file_path = str(secret)
        db.get(Clip, 2).file_path = str(queue.h.ctx.config.clips_dir / ".." / ".." / "x.mp4")
    assert call(queue, "GET", "/clips/1/video").status_code == 404
    assert call(queue, "GET", "/clips/2/video").status_code == 404


def test_edit_title_and_caption(queue):
    ok = call(
        queue, "PATCH", "/clips/1", json={"title": "  He   actually did it ", "caption": "#gta6"}
    )
    assert ok.status_code == 200
    assert (ok.json()["title"], ok.json()["caption"]) == ("He actually did it", "#gta6")
    assert call(queue, "PATCH", "/clips/1", json={"title": ""}).status_code == 422
    assert call(queue, "PATCH", "/clips/1", json={"title": "   "}).status_code == 422
    assert call(queue, "PATCH", "/clips/1", json={"title": "x" * 101}).status_code == 422
    assert call(queue, "PATCH", "/clips/1", json={"title": "He actually did it"}).status_code == 200
    assert audit(queue).count("clip.edit") == 1  # the unchanged save is not logged


def test_approve_exports_video_and_credit(queue, tiny_video):
    call(
        queue,
        "PATCH",
        "/clips/1",
        json={"title": 'W/L? "insane" <clip>: part 1', "caption": "#gta6"},
    )
    response = call(queue, "POST", "/clips/1/approve")
    body = response.json()
    assert response.status_code == 200 and body["status"] == "approved"
    assert body["export_error"] is None

    exported = Path(body["meta"]["exported_path"])
    assert exported.parent == queue.export_dir
    assert exported.name == "shroud - W L insane clip part 1 [1].mp4"
    assert exported.read_bytes() == tiny_video.read_bytes()
    notes = exported.with_suffix(".txt").read_text()
    assert 'W/L? "insane" <clip>: part 1' in notes and "#gta6" in notes
    assert "Clip from shroud: https://www.twitch.tv/shroud" in notes  # every post credits
    assert not list(queue.export_dir.glob("*.part"))

    counts = call(queue, "GET", "/clips").json()["counts"]
    assert counts == {"pending": 2, "approved": 1}
    assert audit(queue)[-2:] == ["clip.approve", "clip.export"]
    assert call(queue, "POST", "/clips/1/approve").status_code == 200  # twice is harmless
    assert audit(queue).count("clip.approve") == 1


def test_approve_survives_an_unwritable_export_folder(queue, tmp_path):
    blocker = tmp_path / "a-file"
    blocker.write_text("x")
    call(queue, "PATCH", "/settings/app", json={"export_dir": str(blocker / "sub")})
    body = call(queue, "POST", "/clips/1/approve").json()
    assert body["status"] == "approved" and "could not write" in body["export_error"]
    assert call(queue, "POST", "/clips/1/export").status_code == 500

    call(queue, "PATCH", "/settings/app", json={"export_dir": str(queue.export_dir)})
    retry = call(queue, "POST", "/clips/1/export")
    assert retry.status_code == 200 and Path(retry.json()["meta"]["exported_path"]).exists()
    assert call(queue, "POST", "/clips/2/export").status_code == 409  # still pending


def test_reject_and_restore(queue):
    assert call(queue, "POST", "/clips/2/reject").json()["status"] == "rejected"
    assert [c["id"] for c in call(queue, "GET", "/clips").json()["clips"]] == [1, 3]
    rejected = call(queue, "GET", "/clips", params={"status": "rejected"}).json()["clips"]
    assert [c["id"] for c in rejected] == [2]
    assert list(queue.export_dir.glob("*")) == []  # rejecting exports nothing
    assert call(queue, "POST", "/clips/2/restore").json()["status"] == "pending"
    assert audit(queue) == ["settings.update", "clip.reject", "clip.restore"]
    assert call(queue, "POST", "/clips/99/reject").status_code == 404


def test_published_clips_cannot_be_flipped_back(queue):
    with queue.h.ctx.session_factory.begin() as db:
        db.get(Clip, 1).status = "published"
    for action in ("approve", "reject", "restore"):
        assert call(queue, "POST", f"/clips/1/{action}").status_code == 409


def test_rerender_needs_the_renderer(queue):
    assert call(queue, "POST", "/clips/1/rerender").status_code == 503


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Normal title", "Normal title"),
        ('a/b\\c:d*e?f"g<h>i|j', "a b c d e f g h i j"),
        ("  trailing dots... ", "trailing dots"),
        ("CON", "clip"),
        ("", "clip"),
        ("../../etc/passwd", "etc passwd"),
        ("x" * 200, "x" * 80),
        ("line\nbreak\ttab", "line break tab"),
    ],
)
def test_safe_file_names(text, expected):
    assert safe_name(text) == expected
