"""Recorder: while a streamer is live, keep a rolling buffer of their stream on disk.

One worker thread per live session reads the stream and feeds it to ffmpeg, which cuts
it into fixed-length segments without re-encoding. A reconcile loop starts workers for
open sessions, stops them when sessions end, restarts them if the stream drops, and
prunes old footage.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from engine.db.audit import write_audit
from engine.db.models import Streamer, StreamSession
from engine.media.ffmpeg import ffmpeg_path, no_window_kwargs
from engine.monitor.urls import StreamerRef
from engine.recorder.store import PruneResult, SegmentStore
from engine.settings import SettingsStore

log = logging.getLogger(__name__)

RECONCILE_INTERVAL_S = 5.0
_READ_SIZE = 64 * 1024
_MAX_BACKOFF_S = 60.0


class StreamUnavailable(Exception):
    """The stream could not be opened (offline, wrong quality, platform trouble)."""


class StreamHandle(Protocol):
    def read(self, size: int) -> bytes: ...
    def close(self) -> None: ...


class StreamSource(Protocol):
    def open(self, url: str, quality: str) -> StreamHandle: ...


class StreamlinkSource:
    """Reads a live stream with Streamlink (Twitch and Kick plugins)."""

    def open(self, url: str, quality: str) -> StreamHandle:
        from streamlink import Streamlink
        from streamlink.exceptions import StreamlinkError
        from streamlink.options import Options

        session = Streamlink()
        session.set_option("stream-timeout", 30)
        # Skip embedded ad breaks so ads never end up inside a clip.
        options = Options({"disable-ads": True, "low-latency": True})
        try:
            streams = session.streams(url, options)
        except StreamlinkError as exc:
            raise StreamUnavailable(f"{type(exc).__name__}") from None
        for name in [q.strip() for q in quality.split(",") if q.strip()] + ["best"]:
            if name in streams:
                try:
                    return streams[name].open()
                except StreamlinkError as exc:
                    raise StreamUnavailable(f"{type(exc).__name__}") from None
        raise StreamUnavailable("no playable stream")


class _Worker(threading.Thread):
    def __init__(
        self,
        session_id: int,
        url: str,
        folder: Path,
        source: StreamSource,
        quality: str,
        segment_seconds: int,
    ) -> None:
        super().__init__(name=f"recorder-{session_id}", daemon=True)
        self.session_id = session_id
        self.url = url
        self.folder = folder
        self._source = source
        self._quality = quality
        self._segment_seconds = segment_seconds
        self._halt = threading.Event()
        self._handle: StreamHandle | None = None
        self.started_at = time.time()
        self.error: str | None = None
        self.bytes_read = 0

    def stop(self) -> None:
        self._halt.set()
        handle = self._handle
        if handle is not None:
            try:
                handle.close()  # unblocks a pending read
            except Exception:  # noqa: BLE001
                pass

    def run(self) -> None:
        process: subprocess.Popen | None = None
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            self._handle = self._source.open(self.url, self._quality)
            if self._halt.is_set():
                return
            run = int(self.started_at)
            pattern = str(self.folder / f"{run}_%06d.ts")
            segment_list = str(self.folder / f"{run}.csv")
            process = subprocess.Popen(
                [
                    ffmpeg_path(),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-fflags",
                    "+genpts+discardcorrupt",
                    "-i",
                    "pipe:0",
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a:0?",
                    "-c",
                    "copy",
                    "-f",
                    "segment",
                    "-segment_time",
                    str(self._segment_seconds),
                    "-segment_format",
                    "mpegts",
                    "-reset_timestamps",
                    "1",
                    "-segment_list",
                    segment_list,
                    "-segment_list_type",
                    "csv",
                    pattern,
                ],  # fmt: skip
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **no_window_kwargs(),
            )
            assert process.stdin is not None
            while not self._halt.is_set():
                chunk = self._handle.read(_READ_SIZE)
                if not chunk:
                    self.error = "the stream ended"
                    break
                self.bytes_read += len(chunk)
                process.stdin.write(chunk)
        except StreamUnavailable as exc:
            self.error = f"could not open the stream ({exc})"
        except (BrokenPipeError, OSError, ValueError) as exc:
            if not self._halt.is_set():
                self.error = f"recording stopped ({type(exc).__name__})"
        except Exception as exc:  # noqa: BLE001 - a worker must never take the engine down
            self.error = f"recording failed ({type(exc).__name__})"
            log.exception("recorder worker for session %s crashed", self.session_id)
        finally:
            if self._handle is not None:
                try:
                    self._handle.close()
                except Exception:  # noqa: BLE001
                    pass
            if process is not None:
                try:
                    if process.stdin:
                        process.stdin.close()  # lets ffmpeg finish the last segment
                    process.wait(timeout=10)
                except (OSError, subprocess.TimeoutExpired):
                    process.kill()
                    process.wait()


@dataclass
class _Retry:
    attempts: int = 0
    not_before: float = 0.0
    last_error: str | None = None


class Recorder:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        store: SegmentStore,
        settings: SettingsStore,
        source: StreamSource | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._session_factory = session_factory
        self.store = store
        self._settings = settings
        self._source = source or StreamlinkSource()
        self._clock = clock
        self._workers: dict[int, _Worker] = {}
        self._retries: dict[int, _Retry] = {}
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self.last_prune = PruneResult()

    # -- public surface -------------------------------------------------------------

    def wake(self) -> None:
        if self._loop is not None and self._wake is not None:
            self._loop.call_soon_threadsafe(self._wake.set)

    def on_session_event(self, _kind: str, _session_id: int) -> None:
        """Hook for the live monitor: react now instead of at the next tick."""
        self.wake()

    def is_recording(self, session_id: int) -> bool:
        with self._lock:
            worker = self._workers.get(session_id)
            return worker is not None and worker.is_alive()

    def status(self, session_id: int) -> dict | None:
        with self._lock:
            worker = self._workers.get(session_id)
            retry = self._retries.get(session_id)
        recording = worker is not None and worker.is_alive()
        segments = self.store.segments(session_id)
        if worker is None and retry is None and not segments:
            return None
        return {
            "recording": recording,
            "buffered_seconds": round(sum(seg.duration for seg in segments), 1),
            "bytes": sum(seg.size for seg in segments),
            "error": None if recording else (retry.last_error if retry else None),
        }

    def summary(self) -> dict:
        settings = self._settings.get()
        return {
            "total_bytes": self.last_prune.total_bytes,
            "cap_bytes": int(settings.disk_cap_gb * 1024**3),
            "recording_sessions": sorted(
                sid for sid, worker in self._workers.items() if worker.is_alive()
            ),
        }

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        log.info("recorder started")
        while True:
            self._wake.clear()
            try:
                await asyncio.to_thread(self.reconcile)
            except Exception:
                log.exception("recorder reconcile failed")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=RECONCILE_INTERVAL_S)
            except TimeoutError:
                pass

    async def aclose(self) -> None:
        await asyncio.to_thread(self.stop_all)

    def stop_all(self) -> None:
        with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
        for worker in workers:
            worker.stop()
        for worker in workers:
            worker.join(timeout=15)

    # -- reconcile ------------------------------------------------------------------

    def _open_sessions(self) -> dict[int, str]:
        with self._session_factory() as db:
            rows = db.execute(
                select(StreamSession.id, Streamer.platform, Streamer.username)
                .join(Streamer, Streamer.id == StreamSession.streamer_id)
                .where(StreamSession.ended_at.is_(None), Streamer.enabled.is_(True))
            ).all()
        return {sid: StreamerRef(platform, username).url for sid, platform, username in rows}

    def reconcile(self) -> None:
        settings = self._settings.get()
        self.store.segment_seconds = settings.segment_seconds
        wanted = self._open_sessions()
        now = self._clock()
        to_stop: list[_Worker] = []
        started: list[int] = []

        with self._lock:
            for session_id, worker in list(self._workers.items()):
                if session_id not in wanted:
                    to_stop.append(self._workers.pop(session_id))
                    self._retries.pop(session_id, None)
                elif not worker.is_alive():
                    # The stream dropped while the session is still open: try again, with
                    # a growing pause so a dead stream is not hammered.
                    self._workers.pop(session_id)
                    retry = self._retries.setdefault(session_id, _Retry())
                    ran_for = now - worker.started_at
                    retry.attempts = 1 if ran_for > 60 else retry.attempts + 1
                    retry.not_before = now + min(_MAX_BACKOFF_S, 5.0 * 2 ** (retry.attempts - 1))
                    retry.last_error = worker.error or "recording stopped"
                    log.warning("recording of session %s stopped: %s", session_id, worker.error)
            for session_id in list(self._retries):
                if session_id not in wanted:
                    del self._retries[session_id]
            for session_id, url in wanted.items():
                if session_id in self._workers:
                    continue
                retry = self._retries.get(session_id)
                if retry and now < retry.not_before:
                    continue
                worker = _Worker(
                    session_id,
                    url,
                    self.store.session_dir(session_id),
                    self._source,
                    settings.stream_quality,
                    settings.segment_seconds,
                )
                self._workers[session_id] = worker
                worker.start()
                started.append(session_id)
            active = set(self._workers)

        for worker in to_stop:
            worker.stop()
        for worker in to_stop:
            worker.join(timeout=15)
        if started:
            self._record_buffer_paths(started)

        result = self.store.prune(
            now=now,
            active=active,
            keep_seconds=settings.buffer_minutes * 60,
            ended_retention_seconds=settings.raw_retention_hours * 3600,
            cap_bytes=int(settings.disk_cap_gb * 1024**3),
        )
        if result.capped and not self.last_prune.capped:
            log.warning("disk cap reached: deleting the oldest footage early")
            with self._session_factory.begin() as db:
                write_audit(
                    db,
                    actor="system",
                    action="recorder.disk_cap",
                    details={"deleted_segments": result.capped},
                )
        self.last_prune = result

    def _record_buffer_paths(self, session_ids: list[int]) -> None:
        with self._session_factory.begin() as db:
            for session_id in session_ids:
                stream = db.get(StreamSession, session_id)
                path = str(self.store.session_dir(session_id))
                if stream is not None and stream.buffer_path != path:
                    stream.buffer_path = path
                    write_audit(
                        db,
                        actor="system",
                        action="recorder.start",
                        object_id=f"session:{session_id}",
                    )
