"""Real renders with ffmpeg, including the Phase 4 checks: under 90 seconds on CPU,
and duplicate moments skipped."""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from engine.config import Config
from engine.control.spend import SpendLedger
from engine.db.models import AuditLog, Clip, Moment, Spend, Streamer, StreamSession
from engine.db.session import create_db_engine, make_session_factory, run_migrations
from engine.media.ffmpeg import ffmpeg_path
from engine.media.probe import probe
from engine.recorder.store import SegmentStore
from engine.render import frames
from engine.render.captions import Word, build_ass
from engine.render.pipeline import ClipPipeline
from engine.render.render import CONCAT_ARGS, CONCAT_LIST, render_clip, write_concat_list
from engine.scorer.transcribe import TranscriptionError, TranscriptionKeyMissing
from engine.settings import SettingsStore
from engine.signals.audio import loudness_per_second
from tests.media import make_buffer

NOW = 1_800_000_000.0


def pixel_rows(video, at, crop):
    """Grey pixels of a region of one frame, as bytes."""
    done = subprocess.run(
        [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-ss", str(at), "-i", str(video),
         "-frames:v", "1", "-vf", f"crop={crop},scale=64:16,format=gray", "-f", "rawvideo", "pipe:1"],
        capture_output=True, check=True,
    )  # fmt: skip
    return done.stdout


@pytest.fixture(scope="module")
def footage(tmp_path_factory):
    """60 seconds of 720p test footage in a buffer, with a loud beep at second 33."""
    root = tmp_path_factory.mktemp("footage")
    store = SegmentStore(root / "buffer")
    make_buffer(store, 1, seconds=60, end_time=NOW, beep_at=33)
    return store


def work_dir(store, tmp_path, start, end):
    work = tmp_path / "work"
    copies = store.extract(1, start, end, work)
    names = [c.path.name for c in copies]
    write_concat_list(work, names)
    return work, names, start - copies[0].start


def test_render_is_vertical_trimmed_accurately_and_loudness_normalised(footage, tmp_path):
    start, end = NOW - 60 + 25, NOW - 60 + 45  # 20 seconds; the beep is 8 s in
    work, names, offset = work_dir(footage, tmp_path, start, end)
    result = render_clip(
        work=work, segment_names=names, offset=offset, duration=20, out=tmp_path / "out.mp4"
    )
    info = probe(result.path)
    assert (info.width, info.height) == (1080, 1920)
    assert info.has_audio and abs(info.duration - 20) < 0.3
    assert result.layout == "blur" and not result.captions

    levels = loudness_per_second(result.path)
    assert levels.index(max(levels)) == 8  # the cut starts where it was asked to
    # Source levels were -46 dB and -6 dB; normalising pulls the quiet part up a lot.
    assert sorted(levels)[len(levels) // 2] > -40


def test_captions_are_burned_in(footage, tmp_path):
    start, end = NOW - 30, NOW - 20
    work, names, offset = work_dir(footage, tmp_path, start, end)
    ass = build_ass([Word("CAPTION", 2.0, 6.0), Word("TEXT", 2.0, 6.0)], 10)
    plain = render_clip(
        work=work, segment_names=names, offset=offset, duration=10, out=tmp_path / "plain.mp4"
    )
    with_text = render_clip(
        work=work,
        segment_names=names,
        offset=offset,
        duration=10,
        out=tmp_path / "text.mp4",
        ass_text=ass,
    )
    assert with_text.captions
    band = "1080:200:0:1280"  # where captions sit

    def changed(at):
        a, b = pixel_rows(plain.path, at, band), pixel_rows(with_text.path, at, band)
        return sum(abs(x - y) > 40 for x, y in zip(a, b, strict=True))

    during, outside = changed(4), changed(8)
    assert during > 100  # visibly different while the caption is up
    assert outside < during / 4  # and not when it is gone


def test_split_layout_puts_the_facecam_on_top(footage, tmp_path):
    start, end = NOW - 30, NOW - 20
    work, names, offset = work_dir(footage, tmp_path, start, end)
    layout = {"facecam": {"x": 0.7, "y": 0.6, "w": 0.3, "h": 0.4}}
    result = render_clip(
        work=work,
        segment_names=names,
        offset=offset,
        duration=10,
        out=tmp_path / "split.mp4",
        layout=layout,
    )
    info = probe(result.path)
    assert (info.width, info.height) == (1080, 1920) and result.layout == "split"


def test_frame_hashes_recognise_the_same_footage(footage, tmp_path):
    work, names, offset = work_dir(footage, tmp_path, NOW - 50, NOW - 10)
    whole = frames.frame_hashes(CONCAT_LIST, CONCAT_ARGS, offset, 40, cwd=work)
    shifted = frames.frame_hashes(CONCAT_LIST, CONCAT_ARGS, offset + 8, 24, cwd=work)
    assert len(whole) == 10 and len(shifted) == 6
    assert frames.is_duplicate(whole, shifted)

    other_store = SegmentStore(tmp_path / "other")
    make_buffer(other_store, 1, seconds=40, end_time=NOW, source="mandelbrot")
    other_work = tmp_path / "other_work"
    copies = other_store.extract(1, NOW - 40, NOW, other_work)
    write_concat_list(other_work, [c.path.name for c in copies])
    different = frames.frame_hashes(CONCAT_LIST, CONCAT_ARGS, 0, 40, cwd=other_work)
    assert not frames.is_duplicate(whole, different)


def test_40_second_1080p60_clip_renders_in_under_90_seconds(tmp_path):
    """Phase 4 check. Busy 1080p60 source (noise on top, so it is not trivially easy to
    encode), with captions, on this machine's CPU."""
    store = SegmentStore(tmp_path / "buffer")
    make_buffer(store, 1, seconds=50, end_time=NOW, size="1920x1080", fps=60)
    work = tmp_path / "work"
    copies = store.extract(1, NOW - 45, NOW - 5, work)
    names = [c.path.name for c in copies]
    words = [Word(f"word{i}", i * 0.5, i * 0.5 + 0.4) for i in range(80)]
    result = render_clip(
        work=work,
        segment_names=names,
        offset=(NOW - 45) - copies[0].start,
        duration=40,
        out=tmp_path / "out.mp4",
        ass_text=build_ass(words, 40),
    )
    print(f"\n40 s 1080p60 clip rendered in {result.seconds_taken:.1f} s on {os.cpu_count()} cores")
    assert abs(result.duration - 40) < 0.3
    assert result.seconds_taken < 90


# -- the pipeline -----------------------------------------------------------------------


class FakeTranscriber:
    def __init__(self):
        self.calls = 0
        self.error: Exception | None = None

    def transcribe(self, audio):
        self.calls += 1
        assert audio.exists() and audio.stat().st_size > 1000
        if self.error:
            raise self.error
        return [
            Word("No", 28.0, 28.3),
            Word("way!", 28.3, 28.8),
            Word("Clip", 30.0, 30.3),
            Word("that", 30.3, 30.6),
        ]


@pytest.fixture
def rig(tmp_path, footage):
    config = Config(data_dir=tmp_path / "data")
    config.ensure_dirs()
    db_engine = create_db_engine(config.db_path)
    run_migrations(db_engine)
    factory = make_session_factory(db_engine)
    with factory.begin() as db:
        db.add(Streamer(platform="twitch", username="shroud", display_name="shroud"))
        db.flush()
        db.add(
            StreamSession(
                streamer_id=1,
                started_at=datetime.fromtimestamp(NOW - 3600, UTC),
                title="GTA VI day one",
            )
        )
    settings = SettingsStore(config.settings_file)
    transcriber = FakeTranscriber()
    clock = [NOW + 5]
    pipeline = ClipPipeline(
        factory, config, settings, footage, SpendLedger(factory), transcriber=transcriber,
        is_recording=lambda sid: True, clock=lambda: clock[0],
    )  # fmt: skip
    yield type(
        "Rig",
        (),
        dict(
            factory=factory,
            config=config,
            settings=settings,
            pipeline=pipeline,
            transcriber=transcriber,
            clock=clock,
        ),
    )
    db_engine.dispose()


def add_moment(rig, peak, scores=None):
    with rig.factory.begin() as db:
        moment = Moment(
            session_id=1,
            peak_ts=datetime.fromtimestamp(peak, UTC),
            signal_scores=scores or {"triggers": ["chat"]},
        )
        db.add(moment)
        db.flush()
        return moment.id


def get(rig, model, ident):
    with rig.factory() as db:
        return db.get(model, ident)


def test_moment_becomes_a_pending_clip(rig):
    heard = []
    rig.pipeline.add_listener(heard.append)
    moment_id = add_moment(rig, peak=NOW - 15)  # window: NOW-45 .. NOW-5
    assert rig.pipeline.process(moment_id) == "clipped"

    with rig.factory() as db:
        clip = db.scalars(select(Clip)).one()
        audit = [a.action for a in db.scalars(select(AuditLog))]
        spend = db.scalars(select(Spend)).one()
    assert heard == [clip.id]
    assert clip.status == "pending" and clip.moment_id == moment_id
    assert clip.title == "GTA VI day one"
    assert clip.transcript == "No way! Clip that"
    assert abs((clip.end_ts - clip.start_ts).total_seconds() - 40) < 0.5
    assert abs(clip.start_ts.timestamp() - (NOW - 45)) < 0.5
    assert clip.meta["captions"] is True and clip.meta["layout"] == "blur"
    assert clip.meta["render_seconds"] < 90
    assert len(frames.decode(clip.phash)) == 10

    video = rig.config.clips_dir / f"clip_{clip.id}.mp4"
    assert clip.file_path == str(video) and video.exists()
    assert video.with_suffix(".jpg").exists()
    assert not list(rig.config.clips_dir.glob("rendering_*"))
    info = probe(video)
    assert (info.width, info.height) == (1080, 1920) and abs(info.duration - 40) < 0.3

    assert get(rig, Moment, moment_id).status == "clipped"
    assert audit == ["clip.render"]
    assert spend.service == "transcription" and spend.usd == pytest.approx(40 / 60 * 0.01)
    # Raw footage for a pending clip is kept even after the retention time.
    rig.clock[0] += 10 * 3600
    for path in (rig.config.raw_dir / f"moment_{moment_id}").iterdir():
        os.utime(path.parent, (NOW, NOW))
    assert rig.pipeline.clean_raw() == 0


def test_duplicate_moments_are_skipped(rig):
    """Phase 4 check."""
    first = add_moment(rig, peak=NOW - 15)
    assert rig.pipeline.process(first) == "clipped"
    renders = rig.transcriber.calls

    overlapping = add_moment(rig, peak=NOW - 22)  # 7 s earlier: mostly the same window
    assert rig.pipeline.process(overlapping) == "rejected"
    moment = get(rig, Moment, overlapping)
    assert (
        moment.status == "rejected" and "same footage as clip 1" in moment.signal_scores["rejected"]
    )
    assert rig.transcriber.calls == renders  # dropped before any paid call or render

    # The same pictures again later in the stream (a replay): caught by frame hashes.
    replay_store = SegmentStore(rig.config.data_dir / "replay")
    make_buffer(replay_store, 1, seconds=60, end_time=NOW + 600, beep_at=33)
    rig.pipeline._store = replay_store
    rig.clock[0] = NOW + 605
    replayed = add_moment(rig, peak=NOW + 600 - 15)
    assert rig.pipeline.process(replayed) == "rejected"
    assert "looks the same as clip 1" in get(rig, Moment, replayed).signal_scores["rejected"]
    assert rig.transcriber.calls == renders

    with rig.factory() as db:
        assert len(db.scalars(select(Clip)).all()) == 1
        actions = [a.action for a in db.scalars(select(AuditLog))]
    assert actions == ["clip.render", "moment.duplicate", "moment.duplicate"]
    assert not (rig.config.raw_dir / f"moment_{replayed}").exists()


def test_waits_for_footage_then_gives_up(rig):
    moment_id = add_moment(rig, peak=NOW - 2)  # needs footage until NOW+8; buffer ends at NOW
    assert rig.pipeline.process(moment_id) == "waiting"
    assert get(rig, Moment, moment_id).status == "detected"
    rig.clock[0] = NOW + 400  # the rest never arrived: clip what there is
    assert rig.pipeline.process(moment_id) == "clipped"
    with rig.factory() as db:
        clip = db.scalars(select(Clip)).one()
    assert 31 < (clip.end_ts - clip.start_ts).total_seconds() < 33


def test_no_footage_is_rejected_with_a_reason(rig):
    moment_id = add_moment(rig, peak=NOW - 5000)
    assert rig.pipeline.process(moment_id) == "rejected"
    assert "no footage" in get(rig, Moment, moment_id).signal_scores["rejected"]
    late = add_moment(rig, peak=NOW - 65)  # only ~5 s of it is in the buffer
    rig.clock[0] = NOW + 400
    assert rig.pipeline.process(late) == "rejected"


@pytest.mark.parametrize(
    ("setup", "note"),
    [
        (
            lambda rig: setattr(rig.transcriber, "error", TranscriptionKeyMissing("x")),
            "no transcription API key is set",
        ),
        (
            lambda rig: setattr(
                rig.transcriber,
                "error",
                TranscriptionError("the transcription service returned 500"),
            ),
            "returned 500",
        ),
        (lambda rig: rig.settings.update({"transcription_daily_cap_usd": 0}), "budget is used up"),
        (lambda rig: rig.settings.update({"captions": False}), "turned off"),
    ],
)
def test_clip_still_renders_without_captions(rig, setup, note):
    setup(rig)
    moment_id = add_moment(rig, peak=NOW - 15)
    assert rig.pipeline.process(moment_id) == "clipped"
    with rig.factory() as db:
        clip = db.scalars(select(Clip)).one()
        spend = db.scalars(select(Spend)).all()
    assert clip.meta["captions"] is False and note in clip.meta["captions_note"]
    assert clip.transcript is None and spend == []


def test_scorer_can_trim_title_and_reject(rig):
    class Trimmer:
        def score(self, draft):
            assert draft.words and draft.duration == pytest.approx(40, abs=0.5)
            assert draft.stream_title == "GTA VI day one"
            draft.cut_start, draft.cut_end = 20.0, 34.0
            draft.title, draft.caption, draft.score = "He actually did it", "#gta6", 0.91
            draft.reasons = ["big reaction"]

    rig.pipeline.scorer = Trimmer()
    moment_id = add_moment(rig, peak=NOW - 15)
    assert rig.pipeline.process(moment_id) == "clipped"
    with rig.factory() as db:
        clip = db.scalars(select(Clip)).one()
    assert (clip.title, clip.caption, clip.claude_score) == ("He actually did it", "#gta6", 0.91)
    assert abs((clip.end_ts - clip.start_ts).total_seconds() - 14) < 0.2
    assert clip.transcript == "No way! Clip that" and clip.meta["reasons"] == ["big reaction"]
    assert abs(probe(clip.file_path).duration - 14) < 0.3

    class Rejecter:
        def score(self, draft):
            draft.score, draft.reject = 0.12, "scored too low (0.12)"

    rig.pipeline.scorer = Rejecter()
    rig.pipeline._store = SegmentStore(rig.config.data_dir / "later")
    make_buffer(rig.pipeline._store, 1, seconds=60, end_time=NOW + 900, source="mandelbrot")
    rig.clock[0] = NOW + 905
    low = add_moment(rig, peak=NOW + 900 - 15)
    assert rig.pipeline.process(low) == "rejected"
    moment = get(rig, Moment, low)
    assert (
        moment.signal_scores["claude_score"] == 0.12
        and "too low" in moment.signal_scores["rejected"]
    )
    with rig.factory() as db:
        assert len(db.scalars(select(Clip)).all()) == 1  # nothing was rendered for it


def test_run_pending_survives_a_crashing_moment_and_cleans_old_raw(rig):
    bad = add_moment(rig, peak=NOW - 15)
    rig.pipeline._store = None  # makes processing blow up
    rig.pipeline.run_pending()
    moment = get(rig, Moment, bad)
    assert moment.status == "rejected" and "internal error" in moment.signal_scores["rejected"]

    stale = rig.config.raw_dir / "moment_999"
    stale.mkdir(parents=True)
    os.utime(stale, (NOW - 8 * 3600, NOW - 8 * 3600))
    fresh = rig.config.raw_dir / "moment_998"
    fresh.mkdir()
    os.utime(fresh, (NOW, NOW))
    assert rig.pipeline.clean_raw() == 1
    assert not stale.exists() and fresh.exists()
