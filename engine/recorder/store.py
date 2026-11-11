"""Rolling segment buffer on disk.

Layout: <buffer_root>/<session id>/<run>_<index>.ts plus <run>.csv, both written by
ffmpeg's segmenter. The CSV lists each finished segment with its exact position in the
stream; the file being written is not listed yet. Stream positions are mapped to clock
time using when each file was closed, so the folder itself is the index and restarts
are safe.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

SEGMENT_SUFFIX = ".ts"
EXTRACT_INDEX = "extract.index"
_OFFSET_WINDOW = 6  # segments either side used to steady the clock mapping


@dataclass(frozen=True)
class Segment:
    path: Path
    start: float  # unix time
    end: float
    size: int

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class PruneResult:
    rolled: int = 0  # older than the rolling window
    expired_sessions: int = 0  # whole buffers of streams that ended long ago
    capped: int = 0  # deleted to get under the disk cap
    total_bytes: int = 0


class SegmentStore:
    def __init__(self, buffer_root: Path, segment_seconds: float = 10.0) -> None:
        self.root = buffer_root
        self.segment_seconds = segment_seconds

    def session_dir(self, session_id: int) -> Path:
        return self.root / str(session_id)

    def session_ids(self) -> list[int]:
        if not self.root.exists():
            return []
        return sorted(int(p.name) for p in self.root.iterdir() if p.is_dir() and p.name.isdigit())

    def _files(self, session_id: int) -> list[tuple[Path, os.stat_result]]:
        return self._folder_files(self.session_dir(session_id))

    @staticmethod
    def _folder_files(folder: Path) -> list[tuple[Path, os.stat_result]]:
        if not folder.exists():
            return []
        files = []
        for path in folder.iterdir():
            if path.suffix == SEGMENT_SUFFIX:
                try:
                    files.append((path, path.stat()))
                except FileNotFoundError:
                    continue  # pruned between listing and stat
        files.sort(key=lambda item: item[0].name)
        return files

    def segments(self, session_id: int) -> list[Segment]:
        """Finished segments, oldest first, with clock times."""
        return self.folder_segments(self.session_dir(session_id))

    def folder_segments(self, folder: Path) -> list[Segment]:
        """The same, for any folder laid out like a session's buffer folder (the raw
        footage copied out for a moment is one)."""
        files = dict((path.name, stat) for path, stat in self._folder_files(folder))
        runs: dict[str, list[tuple[str, float, float]]] = {}
        for name, c_start, c_end in self._listed(folder):
            if name in files:
                runs.setdefault(name.split("_", 1)[0], []).append((name, c_start, c_end))

        result: list[Segment] = []
        for run in sorted(runs):
            entries = runs[run]
            # Data arrives in bursts, so a file can be closed late but never early. The
            # smallest (closed-at minus stream position) nearby is the best clock offset.
            offsets = [files[name].st_mtime - c_end for name, _s, c_end in entries]
            for index, (name, c_start, c_end) in enumerate(entries):
                lo, hi = max(0, index - _OFFSET_WINDOW), index + _OFFSET_WINDOW + 1
                offset = min(offsets[lo:hi])
                result.append(
                    Segment(
                        path=folder / name,
                        start=offset + c_start,
                        end=offset + c_end,
                        size=files[name].st_size,
                    )
                )
        result.sort(key=lambda seg: seg.start)
        return result

    @staticmethod
    def _listed(folder: Path) -> list[tuple[str, float, float]]:
        rows = []
        if not folder.exists():
            return rows
        for csv_path in sorted(folder.glob("*.csv")):
            try:
                lines = csv_path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line in lines:
                parts = line.rsplit(",", 2)
                if len(parts) != 3:
                    continue
                try:
                    rows.append((Path(parts[0]).name, float(parts[1]), float(parts[2])))
                except ValueError:
                    continue  # a line that is still being written
        return rows

    def window(self, session_id: int, start: float, end: float) -> list[Segment]:
        """Segments overlapping [start, end]."""
        return [seg for seg in self.segments(session_id) if seg.end > start and seg.start < end]

    def extract(self, session_id: int, start: float, end: float, dest: Path) -> list[Segment]:
        """Copy the segments covering [start, end] out of the rolling buffer so pruning
        cannot remove them. Returns the copies, with the original timing."""
        dest.mkdir(parents=True, exist_ok=True)
        copies = []
        for seg in self.window(session_id, start, end):
            target = dest / seg.path.name
            if not target.exists():
                try:
                    os.link(seg.path, target)  # instant, no extra disk
                except OSError:
                    shutil.copy2(seg.path, target)
            copies.append(Segment(path=target, start=seg.start, end=seg.end, size=seg.size))
        if copies:
            # Keep the timing with the footage, so the copy can be read back on its own.
            (dest / EXTRACT_INDEX).write_text(
                "".join(f"{c.path.name},{c.start:.6f},{c.end:.6f}\n" for c in copies),
                encoding="utf-8",
            )
        return copies

    @staticmethod
    def read_extract(folder: Path) -> list[Segment]:
        """Segments previously copied out with `extract`, with their original timing."""
        index = folder / EXTRACT_INDEX
        if not index.exists():
            return []
        result = []
        for line in index.read_text(encoding="utf-8").splitlines():
            parts = line.rsplit(",", 2)
            path = folder / Path(parts[0]).name
            if len(parts) == 3 and path.exists():
                result.append(Segment(path, float(parts[1]), float(parts[2]), path.stat().st_size))
        return result

    def total_bytes(self) -> int:
        return sum(stat.st_size for sid in self.session_ids() for _path, stat in self._files(sid))

    def prune(
        self,
        now: float,
        active: set[int],
        keep_seconds: float,
        ended_retention_seconds: float,
        cap_bytes: int,
    ) -> PruneResult:
        result = PruneResult()
        survivors: list[tuple[float, int, Path, int]] = []  # (mtime, session, path, size)

        for session_id in self.session_ids():
            files = self._files(session_id)
            newest = max((stat.st_mtime for _p, stat in files), default=0.0)
            if session_id not in active and now - newest > ended_retention_seconds:
                shutil.rmtree(self.session_dir(session_id), ignore_errors=True)
                result.expired_sessions += 1
                continue
            for index, (path, stat) in enumerate(files):
                being_written = session_id in active and index == len(files) - 1
                if not being_written and now - stat.st_mtime > keep_seconds:
                    if _remove(path):
                        result.rolled += 1
                    continue
                if not being_written:
                    survivors.append((stat.st_mtime, session_id, path, stat.st_size))
                result.total_bytes += stat.st_size

        # Hard cap across all streamers: drop the oldest footage first.
        survivors.sort()
        for _mtime, _session_id, path, size in survivors:
            if result.total_bytes <= cap_bytes:
                break
            if _remove(path):
                result.capped += 1
                result.total_bytes -= size
        return result


def _remove(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return True
    except OSError as exc:
        log.warning("could not delete %s: %s", path.name, exc)
        return False
