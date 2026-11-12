"""Export approved clips to a folder for manual upload.

Each clip is copied as "<streamer> - <title> [<id>].mp4" with a text file beside it
holding the title, the caption, and the credit line every post must carry.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from engine.policy.rules import build_description

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}  # fmt: skip


class ExportError(Exception):
    pass


@dataclass(frozen=True)
class ExportItem:
    clip_id: int
    video: Path
    title: str
    caption: str | None
    streamer_name: str
    channel_url: str
    rules: dict | None = None  # the streamer's clipping-program rules, if any


def safe_name(text: str, limit: int = 80) -> str:
    """A file name that is valid on Windows, macOS and Linux."""
    cleaned = _UNSAFE.sub(" ", text)
    cleaned = " ".join(cleaned.split()).strip(" .")
    cleaned = cleaned[:limit].rstrip(" .")
    if not cleaned or cleaned.upper() in _RESERVED:
        return "clip"
    return cleaned


def description(item: ExportItem) -> str:
    return build_description(item.caption, item.streamer_name, item.channel_url, item.rules)


def export_clip(item: ExportItem, folder: Path) -> Path:
    if not item.video.exists():
        raise ExportError("the clip's video file is missing")
    try:
        folder.mkdir(parents=True, exist_ok=True)
        base = f"{safe_name(item.streamer_name, 30)} - {safe_name(item.title)} [{item.clip_id}]"
        target = folder / f"{base}.mp4"
        partial = folder / f"{base}.mp4.part"
        shutil.copyfile(item.video, partial)
        partial.replace(target)
        (folder / f"{base}.txt").write_text(
            f"Title:\n{item.title}\n\nDescription:\n{description(item)}\n", encoding="utf-8"
        )
    except OSError as exc:
        raise ExportError(
            f"could not write to {folder} ({exc.strerror or type(exc).__name__})"
        ) from None
    return target
