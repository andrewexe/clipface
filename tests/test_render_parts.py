"""Captions, layout filter graphs, frame hashes, transcription client, spend ledger."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from engine.control.spend import SpendCapReached, SpendLedger
from engine.db.session import create_db_engine, make_session_factory, run_migrations
from engine.render import frames
from engine.render.captions import Word, build_ass, chunk
from engine.render.layout import build_filter, valid_facecam
from engine.scorer.transcribe import (
    DeepgramTranscriber,
    TranscriptionError,
    TranscriptionKeyMissing,
)


def words(*items):
    return [Word(text, start, end) for text, start, end in items]


# -- captions -------------------------------------------------------------------------


def test_chunks_are_short_and_follow_speech():
    spoken = words(
        ("No", 0.0, 0.2), ("way", 0.25, 0.5), ("he", 0.55, 0.7), ("just", 0.75, 0.95),
        ("did", 1.0, 1.2), ("that.", 1.25, 1.6), ("Unbelievable", 3.0, 3.8),
    )  # fmt: skip
    lines = chunk(spoken)
    assert [line.text for line in lines] == ["No way he", "just did that.", "Unbelievable"]
    assert all(len(line.text.split()) <= 3 for line in lines)
    assert lines[0].end <= lines[1].start  # never two captions at once
    assert lines[1].end < 2.5  # not held across the long pause


def test_sentence_end_and_long_words_break_lines():
    lines = chunk(words(("Stop.", 0, 0.3), ("Go", 0.35, 0.5), ("incomprehensibilities", 0.6, 1.5)))
    assert [line.text for line in lines] == ["Stop.", "Go", "incomprehensibilities"]


def test_ass_output_and_untrusted_text_is_neutralised():
    hostile = words(
        ("{\\an8}hi", 0.0, 0.5), ("a\\Nb", 0.6, 0.9), ("ok\nDialogue: 0,evil", 1.0, 1.4)
    )
    ass = build_ass(hostile, clip_duration=10)
    events = [line for line in ass.splitlines() if line.startswith("Dialogue:")]
    assert len(events) == 2  # two caption lines; the injected "Dialogue:" is just text
    assert events[1].endswith(",,OK DIALOGUE: 0,EVIL")
    for event in events:
        text = event.split(",,", 1)[1].split(",", 3)[-1]
        assert "{" not in text and "}" not in text and "\\" not in text
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass
    assert events[0].startswith("Dialogue: 0,0:00:00.00,")


def test_ass_clips_to_duration_and_handles_nothing_to_show():
    assert build_ass([], 10) is None
    assert build_ass(words(("late", 20, 21)), 10) is None
    ass = build_ass(words(("edge", 9.5, 12.0)), 10)
    assert ",0:00:10.00," in ass  # end clamped to the clip


# -- layout ---------------------------------------------------------------------------


def test_blur_layout_is_the_default():
    graph = build_filter(1920, 1080, None, None)
    assert "boxblur" in graph and "overlay" in graph and graph.endswith("[v]")
    assert "scale=1080:608" in graph  # the full frame, full width
    assert "ass=" not in graph
    assert "ass=captions.ass" in build_filter(1920, 1080, None, "captions.ass")


def test_split_layout_uses_the_facecam_rectangle():
    layout = {"facecam": {"x": 0.75, "y": 0.70, "w": 0.25, "h": 0.30}}
    graph = build_filter(1920, 1080, layout, None)
    assert "crop=480:324:1440:756" in graph and "vstack" in graph
    cam_h = int(graph.split("scale=1080:")[1].split(":")[0])
    assert 420 <= cam_h <= 860 and f"scale=-2:{1920 - cam_h}" in graph


@pytest.mark.parametrize(
    "layout",
    [None, {}, {"facecam": None}, {"facecam": "x"}, {"facecam": {"x": 0.1}},
     {"facecam": {"x": 2, "y": 0, "w": 0.5, "h": 0.5}},
     {"facecam": {"x": 0, "y": 0, "w": 0.01, "h": 0.5}},
     {"facecam": {"x": "a", "y": 0, "w": 0.5, "h": 0.5}}],
)  # fmt: skip
def test_bad_facecam_falls_back_to_blur(layout):
    assert valid_facecam(layout) is None
    assert "boxblur" in build_filter(1280, 720, layout, None)


def test_facecam_is_clamped_to_the_frame():
    cam = valid_facecam({"facecam": {"x": 0.8, "y": 0.8, "w": 0.5, "h": 0.5}})
    assert cam["x"] + cam["w"] <= 1 and cam["y"] + cam["h"] <= 1


# -- frame hashes ---------------------------------------------------------------------


def test_hash_encoding_round_trips():
    values = [0, 1, 2**64 - 1, 0x0123456789ABCDEF]
    assert frames.decode(frames.encode(values)) == values
    assert (
        frames.decode(None) == [] and frames.decode("xyz") == [] and frames.decode("zz" * 8) == []
    )


def test_similarity_rules():
    a = [0xFFFF0000FFFF0000, 0x00FF00FF00FF00FF, 0x123456789ABCDEF0, 0x0F0F0F0F0F0F0F0F]
    near = [value ^ 0b111 for value in a]  # three bits flipped per frame
    other = [0x5555555555555555, 0xAAAAAAAAAAAAAAAA, 0x3333333333333333, 0xCCCCCCCCCCCCCCCC]
    assert frames.is_duplicate(a, near)
    assert not frames.is_duplicate(a, other)
    assert frames.is_duplicate(a[:2], a)  # a shorter clip of the same footage
    assert not frames.is_duplicate([], a)
    assert not frames.is_duplicate([0, 0, 0], [0, 0, 0])  # black frames prove nothing


def test_audio_match_allows_a_shift_and_rejects_flat_or_different_sound():
    curve = (
        [-40.0] * 12 + [-8.0, -10.0, -15.0, -30.0] + [-40.0] * 12 + [-20.0, -12.0] + [-40.0] * 10
    )
    assert frames.audio_matches(curve, curve)
    assert frames.audio_matches(curve, curve[7:] + [-40.0] * 7)  # same sound, 7 s later
    assert frames.audio_matches(curve, [level + 6 for level in curve])  # louder copy
    other = [-40.0] * 5 + [-9.0] * 3 + [-40.0] * 20 + [-11.0] * 4 + [-40.0] * 8
    assert not frames.audio_matches(curve, other)
    flat = [-35.0] * 40
    assert not frames.audio_matches(flat, flat)  # silence proves nothing
    assert not frames.audio_matches(curve, []) and not frames.audio_matches([], [])
    same_frames = [0xFFFF0000FFFF0000, 0x00FF00FF00FF00FF]
    assert frames.same_footage(same_frames, curve, same_frames, curve)
    assert not frames.same_footage(same_frames, curve, same_frames, other)


# -- transcription client ---------------------------------------------------------------


def deepgram(handler, key="dg-key"):
    return DeepgramTranscriber(lambda: key, transport=httpx.MockTransport(handler))


def test_deepgram_request_and_word_parsing(tmp_path):
    audio = tmp_path / "a.flac"
    audio.write_bytes(b"fLaC....")
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["Authorization"]
        seen["type"] = request.headers["Content-Type"]
        seen["body"] = request.content
        alt = {
            "transcript": "no way",
            "words": [
                {
                    "word": "no",
                    "start": 0.08,
                    "end": 0.32,
                    "confidence": 0.99,
                    "punctuated_word": "No",
                },
                {"word": "way", "start": 0.32, "end": 0.8, "confidence": 0.98},
                {"word": "", "start": 1, "end": 2},
                {"start": 1, "end": 2},
                {"word": "backwards", "start": 3, "end": 2},
            ],
        }
        return httpx.Response(200, json={"results": {"channels": [{"alternatives": [alt]}]}})

    result = deepgram(handler).transcribe(audio)
    assert result == [Word("No", 0.08, 0.32), Word("way", 0.32, 0.8)]
    assert seen["url"].startswith("https://api.deepgram.com/v1/listen?")
    assert "model=nova-3" in seen["url"] and "smart_format=true" in seen["url"]
    assert seen["auth"] == "Token dg-key" and seen["type"] == "audio/flac"
    assert seen["body"] == b"fLaC...."


def test_deepgram_failures(tmp_path):
    audio = tmp_path / "a.flac"
    audio.write_bytes(b"x")
    with pytest.raises(TranscriptionKeyMissing):
        deepgram(lambda r: httpx.Response(200), key=None).transcribe(audio)
    with pytest.raises(TranscriptionError, match="rejected the API key"):
        deepgram(lambda r: httpx.Response(401, json={"err_msg": "bad"})).transcribe(audio)
    with pytest.raises(TranscriptionError, match="returned 500"):
        deepgram(lambda r: httpx.Response(500)).transcribe(audio)
    with pytest.raises(TranscriptionError, match="unreadable"):
        deepgram(lambda r: httpx.Response(200, json={"results": {}})).transcribe(audio)

    def boom(request):
        raise httpx.ReadTimeout("slow")

    with pytest.raises(TranscriptionError) as excinfo:
        deepgram(boom, key="super-secret-key").transcribe(audio)
    assert "super-secret-key" not in str(excinfo.value)


# -- spend ledger -----------------------------------------------------------------------


def test_daily_cap(tmp_path):
    db_engine = create_db_engine(tmp_path / "l.db")
    run_migrations(db_engine)
    clock = [datetime(2026, 11, 19, 23, 0, tzinfo=UTC)]
    ledger = SpendLedger(make_session_factory(db_engine), now=lambda: clock[0])
    ledger.check("transcription", cap_usd=1.0, about_to_spend=0.4)
    ledger.record("transcription", 0.4)
    ledger.record("transcription", 0.5)
    ledger.record("anthropic", 9.0)  # another service's spend is separate
    assert ledger.spent_today("transcription") == pytest.approx(0.9)
    ledger.check("transcription", cap_usd=1.0, about_to_spend=0.1)
    with pytest.raises(SpendCapReached) as excinfo:
        ledger.check("transcription", cap_usd=1.0, about_to_spend=0.2)
    assert excinfo.value.spent == pytest.approx(0.9)
    clock[0] += timedelta(hours=2)  # past midnight UTC: a new day
    ledger.check("transcription", cap_usd=1.0, about_to_spend=0.9)
    with pytest.raises(SpendCapReached):
        ledger.check("transcription", cap_usd=0.0, about_to_spend=0.01)  # cap of zero: off
    db_engine.dispose()
