"""Read a media file's basic facts from ffmpeg's own report (no ffprobe needed)."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from engine.media.ffmpeg import ffmpeg_path, no_window_kwargs


@dataclass(frozen=True)
class MediaInfo:
    duration: float | None
    width: int | None
    height: int | None
    fps: float | None
    has_video: bool
    has_audio: bool


def probe(path: Path, extra_input_args: list[str] | None = None) -> MediaInfo:
    done = subprocess.run(
        [ffmpeg_path(), "-hide_banner", *(extra_input_args or []), "-i", str(path)],
        capture_output=True,
        text=True,
        errors="replace",
        **no_window_kwargs(),
    )
    out = done.stderr
    duration = None
    match = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", out)
    if match:
        hours, minutes, seconds = match.groups()
        duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    video = re.search(r"Stream #.*Video:.*", out)
    width = height = None
    fps = None
    if video:
        size = re.search(r"[ ,](\d{2,5})x(\d{2,5})[ ,\[]", video.group(0))
        if size:
            width, height = int(size.group(1)), int(size.group(2))
        rate = re.search(r"([\d.]+) fps", video.group(0))
        if rate:
            fps = float(rate.group(1))
    return MediaInfo(
        duration=duration,
        width=width,
        height=height,
        fps=fps,
        has_video=video is not None,
        has_audio="Audio:" in out,
    )
