"""Audio loudness per second of a recorded segment, in dBFS.

Shouting, laughter and sudden noise show up as loudness spikes. This signal needs no
chat, so it also works for Kick, whose public API has no way to read chat.
"""

from __future__ import annotations

import array
import math
import subprocess
from pathlib import Path

from engine.media.ffmpeg import ffmpeg_path, no_window_kwargs

SAMPLE_RATE = 8000
SILENCE_DB = -90.0


def loudness_per_second(path: Path, timeout_s: float = 30.0) -> list[float]:
    """RMS level of each second of audio. Empty if the file has no readable audio."""
    try:
        done = subprocess.run(
            [
                ffmpeg_path(),
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-i",
                str(path),
                "-vn",
                "-ac",
                "1",
                "-ar",
                str(SAMPLE_RATE),
                "-f",
                "s16le",
                "pipe:1",
            ],  # fmt: skip
            capture_output=True,
            timeout=timeout_s,
            **no_window_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    samples = array.array("h")
    samples.frombytes(done.stdout[: len(done.stdout) // 2 * 2])
    levels = []
    for start in range(0, len(samples), SAMPLE_RATE):
        chunk = samples[start : start + SAMPLE_RATE]
        if len(chunk) < SAMPLE_RATE // 2:
            break
        mean_square = sum(sample * sample for sample in chunk) / len(chunk)
        levels.append(
            max(SILENCE_DB, 10 * math.log10(mean_square / 32768.0**2))
            if mean_square
            else SILENCE_DB
        )
    return levels
