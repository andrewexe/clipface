"""Clip pipeline: detected moment -> footage -> dedupe -> transcript -> render -> clip.

Runs one moment at a time. Every outcome is written back: the moment becomes "clipped"
(with a pending clip for review) or "rejected" (with the reason).
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from engine.config import Config
from engine.control.spend import SpendCapReached, SpendLedger
from engine.db.audit import write_audit
from engine.db.models import Clip, Moment, Streamer, StreamSession
from engine.recorder.store import SegmentStore
from engine.render import frames
from engine.render.captions import Word, build_ass
from engine.render.render import (
    CONCAT_ARGS,
    CONCAT_LIST,
    RenderError,
    extract_audio,
    make_thumbnail,
    render_clip,
    write_concat_list,
)
from engine.scorer.transcribe import (
    Transcriber,
    TranscriptionError,
    TranscriptionKeyMissing,
)
from engine.settings import AppSettings, SettingsStore
from engine.signals.audio import loudness_per_second

log = logging.getLogger(__name__)

FOOTAGE_WAIT_S = 180  # how long after the peak we keep waiting for footage to land
MIN_CLIP_S = 8.0
DEDUPE_LOOKBACK_H = 24
WORDS_FILE = "words.json"
WINDOW_FILE = "window.json"


@dataclass
class Draft:
    """What is known about a clip before it is rendered. A scorer may change it."""

    moment_id: int
    streamer_name: str
    stream_title: str | None
    game: str | None
    window_start: float  # unix time of the first available frame of the window
    duration: float
    words: list[Word]
    signal_scores: dict
    # Decisions (defaults: use the whole window)
    cut_start: float = 0.0  # seconds into the window
    cut_end: float | None = None
    title: str | None = None
    caption: str | None = None
    score: float | None = None
    reasons: list[str] = field(default_factory=list)
    reject: str | None = None  # set to drop the moment before rendering
    notes: dict[str, str] = field(default_factory=dict)  # e.g. why it was not scored


class Scorer(Protocol):
    def score(self, draft: Draft) -> None:
        """Fill in cut, title, caption, score. Set draft.reject to drop the moment."""


class ClipPipeline:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        config: Config,
        settings: SettingsStore,
        store: SegmentStore,
        ledger: SpendLedger,
        transcriber: Transcriber | None = None,
        scorer: Scorer | None = None,
        is_recording: Callable[[int], bool] = lambda _session_id: False,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._session_factory = session_factory
        self._config = config
        self._settings = settings
        self._store = store
        self._ledger = ledger
        self._transcriber = transcriber
        self.scorer = scorer
        self._is_recording = is_recording
        self._clock = clock
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self._listeners: list[Callable[[int], None]] = []
        self._last_janitor = 0.0

    def add_listener(self, listener: Callable[[int], None]) -> None:
        """Called with the clip id each time a clip is rendered."""
        self._listeners.append(listener)

    def wake(self) -> None:
        if self._loop is not None and self._wake is not None:
            self._loop.call_soon_threadsafe(self._wake.set)

    def on_moment(self, _moment_id: int) -> None:
        self.wake()

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        log.info("clip pipeline started")
        while True:
            self._wake.clear()
            try:
                await asyncio.to_thread(self.run_pending)
            except Exception:
                log.exception("clip pipeline pass failed")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=3.0)
            except TimeoutError:
                pass

    async def aclose(self) -> None:
        pass

    # -- one pass -------------------------------------------------------------------

    def run_pending(self) -> None:
        with self._session_factory() as db:
            ids = db.scalars(
                select(Moment.id).where(Moment.status == "detected").order_by(Moment.id)
            ).all()
        for moment_id in ids:
            try:
                self.process(moment_id)
            except Exception as exc:  # noqa: BLE001 - one bad moment must not stop the rest
                log.exception("moment %s failed", moment_id)
                self._reject(moment_id, f"internal error ({type(exc).__name__})")
        now = self._clock()
        if now - self._last_janitor > 60:
            self._last_janitor = now
            self.clean_raw()

    def process(self, moment_id: int) -> str:
        """Returns "waiting", "rejected" or "clipped"."""
        settings = self._settings.get()
        with self._session_factory() as db:
            moment = db.get(Moment, moment_id)
            if moment is None or moment.status != "detected":
                return "rejected"
            stream = db.get(StreamSession, moment.session_id)
            streamer = db.get(Streamer, stream.streamer_id)
            peak = moment.peak_ts.timestamp()
            session_id, session_open = stream.id, stream.ended_at is None
            streamer_id, layout = streamer.id, streamer.layout
            draft_base = {
                "streamer_name": streamer.display_name or streamer.username,
                "stream_title": stream.title,
                "game": stream.game,
                "signal_scores": dict(moment.signal_scores or {}),
            }

        want_start, want_end = peak - settings.clip_before_s, peak + settings.clip_after_s
        segments = self._store.window(session_id, want_start, want_end)
        covered_to = segments[-1].end if segments else 0.0
        if covered_to < want_end - 0.5:
            still_coming = session_open and self._is_recording(session_id)
            if still_coming and self._clock() - peak < FOOTAGE_WAIT_S:
                return "waiting"
        if not segments:
            return self._reject(moment_id, "no footage of this moment was recorded")

        start = max(want_start, segments[0].start)
        end = min(want_end, covered_to)
        if end - start < MIN_CLIP_S:
            return self._reject(moment_id, "too little footage of this moment was recorded")

        duplicate = self._overlapping_clip(session_id, start, end)
        if duplicate is not None:
            return self._reject(moment_id, f"same footage as clip {duplicate}", duplicate)

        work = self._config.raw_dir / f"moment_{moment_id}"
        copies = self._store.extract(session_id, start, end, work)
        names = [seg.path.name for seg in copies]
        write_concat_list(work, names)
        offset = start - copies[0].start
        duration = end - start

        hashes = frames.frame_hashes(Path(CONCAT_LIST), CONCAT_ARGS, offset, duration, cwd=work)
        loudness = [
            round(level, 1)
            for level in loudness_per_second(
                Path(CONCAT_LIST), input_args=CONCAT_ARGS, start=offset, duration=duration, cwd=work
            )
        ]
        duplicate = self._lookalike_clip(streamer_id, hashes, loudness)
        if duplicate is not None:
            return self._reject(moment_id, f"looks the same as clip {duplicate}", duplicate)

        words, captions_note = self._transcribe(work, offset, duration, settings)
        draft = Draft(
            moment_id=moment_id,
            window_start=start,
            duration=duration,
            words=words,
            **draft_base,
        )
        if self.scorer is not None:
            self.scorer.score(draft)
            if draft.score is not None:
                self._mark(moment_id, "scored")
            if draft.reject:
                return self._reject(moment_id, draft.reject, score=draft.score)

        cut_start = min(max(0.0, draft.cut_start), max(0.0, duration - MIN_CLIP_S))
        cut_end = min(duration, draft.cut_end if draft.cut_end is not None else duration)
        if cut_end - cut_start < MIN_CLIP_S:
            cut_start, cut_end = 0.0, duration
        cut_words = [
            Word(w.text, w.start - cut_start, w.end - cut_start)
            for w in words
            if w.end > cut_start and w.start < cut_end
        ]
        ass = build_ass(cut_words, cut_end - cut_start) if settings.captions else None

        tmp = self._config.clips_dir / f"rendering_moment_{moment_id}.mp4"
        try:
            result = render_clip(
                work=work,
                segment_names=names,
                offset=offset + cut_start,
                duration=cut_end - cut_start,
                out=tmp,
                layout=layout,
                ass_text=ass,
            )
        except RenderError as exc:
            tmp.unlink(missing_ok=True)
            return self._reject(moment_id, f"render failed: {exc}")

        (work / WINDOW_FILE).write_text(json.dumps({"start": start}), encoding="utf-8")
        (work / WORDS_FILE).write_text(
            json.dumps([[w.text, round(w.start, 3), round(w.end, 3)] for w in words]),
            encoding="utf-8",
        )
        clip_id = self._save_clip(
            moment_id=moment_id,
            start=start + cut_start,
            end=start + cut_end,
            tmp=tmp,
            transcript=" ".join(w.text for w in cut_words) or None,
            draft=draft,
            phash=frames.encode(hashes),
            meta={
                "duration": round(result.duration, 2),
                "render_seconds": round(result.seconds_taken, 1),
                "captions": result.captions,
                "captions_note": captions_note,
                "layout": result.layout,
                # Loudness of the whole window, second by second: with the frame hashes,
                # this is how a later clip of the same footage is recognised.
                "loudness": loudness,
                **({"scoring_note": draft.notes["scoring"]} if "scoring" in draft.notes else {}),
            },
        )
        for listener in self._listeners:
            try:
                listener(clip_id)
            except Exception:
                log.exception("clip listener failed")
        return "clipped"

    # -- steps ----------------------------------------------------------------------

    def _transcribe(
        self, work: Path, offset: float, duration: float, settings: AppSettings
    ) -> tuple[list[Word], str | None]:
        """Returns (words, note). The note says why there are no captions, if so."""
        if not settings.captions:
            return [], "captions are turned off"
        if self._transcriber is None:
            return [], "no transcription service is set up"
        cost = duration / 60 * settings.transcription_usd_per_minute
        try:
            self._ledger.check("transcription", settings.transcription_daily_cap_usd, cost)
            audio = extract_audio(work, offset, duration)
            words = self._transcriber.transcribe(audio)
            audio.unlink(missing_ok=True)
        except SpendCapReached:
            return [], "today's transcription budget is used up"
        except TranscriptionKeyMissing:
            return [], "no transcription API key is set"
        except (TranscriptionError, RenderError) as exc:
            log.warning("transcription failed: %s", exc)
            return [], str(exc)
        self._ledger.record("transcription", cost, {"seconds": round(duration, 1)})
        return words, None if words else "no speech was found"

    def _overlapping_clip(self, session_id: int, start: float, end: float) -> int | None:
        """A clip from the same stream that already covers most of this window."""
        with self._session_factory() as db:
            clips = db.execute(
                select(Clip.id, Clip.start_ts, Clip.end_ts)
                .join(Moment, Moment.id == Clip.moment_id)
                .where(Moment.session_id == session_id)
            ).all()
        for clip_id, clip_start, clip_end in clips:
            overlap = min(end, clip_end.timestamp()) - max(start, clip_start.timestamp())
            if overlap > 0.5 * (end - start):
                return clip_id
        return None

    def _lookalike_clip(
        self, streamer_id: int, hashes: list[int], loudness: list[float]
    ) -> int | None:
        """A recent clip of this streamer with the same pictures and the same sound."""
        if not hashes or not loudness:
            return None
        since = datetime.fromtimestamp(self._clock(), UTC) - timedelta(hours=DEDUPE_LOOKBACK_H)
        with self._session_factory() as db:
            clips = db.execute(
                select(Clip.id, Clip.phash, Clip.meta)
                .join(Moment, Moment.id == Clip.moment_id)
                .join(StreamSession, StreamSession.id == Moment.session_id)
                .where(
                    StreamSession.streamer_id == streamer_id,
                    Clip.phash.is_not(None),
                    Clip.start_ts >= since,
                )
            ).all()
        for clip_id, phash, meta in clips:
            other_loudness = (meta or {}).get("loudness") or []
            if frames.same_footage(hashes, loudness, frames.decode(phash), other_loudness):
                return clip_id
        return None

    def _save_clip(
        self,
        *,
        moment_id: int,
        start: float,
        end: float,
        tmp: Path,
        transcript: str | None,
        draft: Draft,
        phash: str,
        meta: dict,
    ) -> int:
        with self._session_factory.begin() as db:
            moment = db.get(Moment, moment_id)
            clip = Clip(
                moment_id=moment_id,
                start_ts=datetime.fromtimestamp(start, UTC),
                end_ts=datetime.fromtimestamp(end, UTC),
                transcript=transcript,
                claude_score=draft.score,
                title=(draft.title or draft.stream_title or f"{draft.streamer_name} clip")[:200],
                caption=draft.caption,
                phash=phash or None,
                status="pending",
                meta={**meta, "reasons": draft.reasons} if draft.reasons else meta,
            )
            db.add(clip)
            db.flush()
            final = self._config.clips_dir / f"clip_{clip.id}.mp4"
            tmp.replace(final)
            clip.file_path = str(final)
            make_thumbnail(final, final.with_suffix(".jpg"), at=meta["duration"] / 2)
            moment.status = "clipped"
            write_audit(
                db,
                actor="system",
                action="clip.render",
                object_id=f"clip:{clip.id}",
                details={"moment_id": moment_id, **meta},
            )
            log.info("clip %s rendered in %.1fs", clip.id, meta["render_seconds"])
            return clip.id

    def _mark(self, moment_id: int, status: str) -> None:
        with self._session_factory.begin() as db:
            moment = db.get(Moment, moment_id)
            if moment is not None:
                moment.status = status

    def _reject(
        self,
        moment_id: int,
        reason: str,
        duplicate_of: int | None = None,
        score: float | None = None,
    ) -> str:
        with self._session_factory.begin() as db:
            moment = db.get(Moment, moment_id)
            if moment is None:
                return "rejected"
            moment.status = "rejected"
            scores = dict(moment.signal_scores or {})
            scores["rejected"] = reason
            if score is not None:
                scores["claude_score"] = score
            moment.signal_scores = scores
            write_audit(
                db,
                actor="system",
                action="moment.duplicate" if duplicate_of is not None else "moment.reject",
                object_id=f"moment:{moment_id}",
                details={"reason": reason},
            )
        log.info("moment %s dropped: %s", moment_id, reason)
        shutil.rmtree(self._config.raw_dir / f"moment_{moment_id}", ignore_errors=True)
        return "rejected"

    def rerender(self, clip_id: int) -> None:
        """Render a clip again from its raw footage, with the streamer's current layout.
        Raises RenderError if the raw footage is gone."""
        settings = self._settings.get()
        with self._session_factory() as db:
            clip = db.get(Clip, clip_id)
            if clip is None:
                raise RenderError("clip not found")
            moment = db.get(Moment, clip.moment_id)
            stream = db.get(StreamSession, moment.session_id)
            layout = db.get(Streamer, stream.streamer_id).layout
            start, end = clip.start_ts.timestamp(), clip.end_ts.timestamp()
            moment_id, final = moment.id, Path(clip.file_path or "")
        work = self._config.raw_dir / f"moment_{moment_id}"
        copies = [
            seg for seg in SegmentStore.read_extract(work) if seg.end > start and seg.start < end
        ]
        if not copies or not final.name:
            raise RenderError("the raw footage for this clip has been deleted")
        names = [seg.path.name for seg in copies]
        offset = max(0.0, start - copies[0].start)
        words: list[Word] = []
        words_file = work / WORDS_FILE
        if settings.captions and words_file.exists():
            window_start = float(json.loads((work / WINDOW_FILE).read_text())["start"])
            shift = start - window_start
            words = [
                Word(text, w_start - shift, w_end - shift)
                for text, w_start, w_end in json.loads(words_file.read_text(encoding="utf-8"))
                if w_end > shift and w_start < shift + (end - start)
            ]
        tmp = final.with_name(f"rendering_clip_{clip_id}.mp4")
        try:
            result = render_clip(
                work=work,
                segment_names=names,
                offset=offset,
                duration=end - start,
                out=tmp,
                layout=layout,
                ass_text=build_ass(words, end - start) if words else None,
            )
        except RenderError:
            tmp.unlink(missing_ok=True)
            raise
        tmp.replace(final)
        make_thumbnail(final, final.with_suffix(".jpg"), at=result.duration / 2)
        with self._session_factory.begin() as db:
            clip = db.get(Clip, clip_id)
            clip.meta = {
                **(clip.meta or {}),
                "layout": result.layout,
                "captions": result.captions,
                "render_seconds": round(result.seconds_taken, 1),
                "exported_path": None,
            }
            write_audit(
                db,
                actor="user",
                action="clip.rerender",
                object_id=f"clip:{clip_id}",
                details={"layout": result.layout},
            )

    # -- housekeeping -----------------------------------------------------------------

    def clean_raw(self) -> int:
        """Delete raw footage older than the retention time, unless a clip that is still
        pending or approved was cut from it."""
        root = self._config.raw_dir
        if not root.exists():
            return 0
        retention = self._settings.get().raw_retention_hours * 3600
        with self._session_factory() as db:
            in_use = set(
                db.scalars(
                    select(Clip.moment_id).where(Clip.status.in_(("pending", "approved")))
                ).all()
            )
        removed = 0
        for folder in root.iterdir():
            if not folder.is_dir() or not folder.name.startswith("moment_"):
                continue
            try:
                moment_id = int(folder.name.split("_", 1)[1])
                age = self._clock() - folder.stat().st_mtime
            except (ValueError, OSError):
                continue
            if moment_id not in in_use and age > retention:
                shutil.rmtree(folder, ignore_errors=True)
                removed += 1
        return removed
