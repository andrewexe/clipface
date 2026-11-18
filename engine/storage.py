"""Keeping disk use down.

- A rejected clip (by you, the safety gate, a stop request or a failed spot check) is
  deleted for good: its video, thumbnail, raw footage, and any exported copy. Its row
  stays, without file paths, so the same footage is still recognised as a duplicate.
- Raw footage of a moment is deleted as soon as no clip waiting for review or posting
  needs it.
- Once a clip is posted everywhere it was scheduled to go, the app's own copy of the
  video is deleted. The exported copy in your export folder stays.
- Exports are hard links where possible, so an exported clip takes no extra space.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from engine.config import Config
from engine.db.models import Clip, Moment, PublishJob

log = logging.getLogger(__name__)

FINAL = ("rejected", "removed")
NEEDS_RAW = ("pending", "approved")  # still reviewable or re-renderable
UNFINISHED_JOBS = ("scheduled", "running", "failed")


@dataclass
class Freed:
    files: int = 0
    bytes: int = 0

    def add(self, other: Freed) -> None:
        self.files += other.files
        self.bytes += other.bytes


def _delete(path: Path) -> Freed:
    try:
        size = path.stat().st_size
        path.unlink()
        return Freed(1, size)
    except FileNotFoundError:
        return Freed()
    except OSError as exc:
        log.warning("could not delete %s: %s", path.name, exc)
        return Freed()


def _delete_tree(path: Path) -> Freed:
    if not path.is_dir():
        return Freed()
    freed = Freed()
    for item in path.rglob("*"):
        if item.is_file():
            try:
                freed.add(Freed(1, item.stat().st_size))
            except OSError:
                pass
    shutil.rmtree(path, ignore_errors=True)
    return freed


def _inside(path: Path, folder: Path) -> bool:
    try:
        return path.resolve().parent == folder.resolve()
    except OSError:
        return False


def purge_clip_files(config: Config, clip: Clip) -> Freed:
    """Delete everything on disk for a rejected clip. Call inside a transaction; it
    clears the clip's file paths."""
    freed = Freed()
    meta = dict(clip.meta or {})
    if clip.file_path:
        video = Path(clip.file_path)
        if _inside(video, config.clips_dir):  # never delete anything outside our folder
            freed.add(_delete(video))
            freed.add(_delete(video.with_suffix(".jpg")))
    exported = meta.get("exported_path")
    if exported:
        copy = Path(exported)
        # Only the file the app itself exported for this clip, recognised by its name.
        if copy.name.endswith(f"[{clip.id}].mp4"):
            freed.add(_delete(copy))
            freed.add(_delete(copy.with_suffix(".txt")))
    freed.add(_delete_tree(config.raw_dir / f"moment_{clip.moment_id}"))
    clip.file_path = None
    meta.pop("exported_path", None)
    meta["files_deleted"] = True
    clip.meta = meta
    return freed


def purge_clip(session_factory: sessionmaker[Session], config: Config, clip_id: int) -> Freed:
    with session_factory.begin() as db:
        clip = db.get(Clip, clip_id)
        if clip is None or clip.status not in FINAL:
            return Freed()
        return purge_clip_files(config, clip)


def sweep(
    session_factory: sessionmaker[Session], config: Config, now: float, retention_s: float
) -> Freed:
    """One pass over everything the app can safely delete."""
    freed = Freed()
    with session_factory.begin() as db:
        # 1. Rejected and removed clips that still have files.
        for clip in db.scalars(select(Clip).where(Clip.status.in_(FINAL))):
            if not (clip.meta or {}).get("files_deleted"):
                freed.add(purge_clip_files(config, clip))
        # 2. Posted clips: the app's copy, once no post is waiting to use it.
        for clip in db.scalars(select(Clip).where(Clip.status == "published")):
            if not clip.file_path:
                continue
            waiting = db.scalar(
                select(PublishJob.id)
                .where(PublishJob.clip_id == clip.id, PublishJob.status.in_(UNFINISHED_JOBS))
                .limit(1)
            )
            video = Path(clip.file_path)
            if waiting is None and _inside(video, config.clips_dir):
                freed.add(_delete(video))  # the thumbnail is tiny and stays
                clip.file_path = None
        needs_raw = set(db.scalars(select(Clip.moment_id).where(Clip.status.in_(NEEDS_RAW))))
        moment_status = dict(db.execute(select(Moment.id, Moment.status)).all())

    # 3. Raw footage no clip needs any more.
    root = config.raw_dir
    if root.exists():
        for folder in root.iterdir():
            if not folder.is_dir() or not folder.name.startswith("moment_"):
                continue
            try:
                moment_id = int(folder.name.split("_", 1)[1])
                age = now - folder.stat().st_mtime
            except (ValueError, OSError):
                continue
            if moment_id in needs_raw:
                continue
            finished = moment_status.get(moment_id) in ("clipped", "rejected")
            if finished or age > retention_s:
                freed.add(_delete_tree(folder))
    if freed.files:
        log.info("storage: deleted %d file(s), %.1f MB", freed.files, freed.bytes / 1024**2)
    return freed


def folder_size(path: Path) -> int:
    total = 0
    if path.is_dir():
        for item in path.rglob("*"):
            try:
                if item.is_file():
                    total += item.stat().st_size
            except OSError:
                continue
    return total
