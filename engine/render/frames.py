"""Perceptual hashes of video frames, for spotting clips of the same footage.

Each sampled frame is shrunk to 9x8 grey pixels and reduced to 64 bits: one bit per
pixel pair, set when the left pixel is brighter (a "difference hash"). Small changes
(re-encoding, captions, a slight shift) flip few bits; different pictures flip about half.

Pictures alone are not enough: a streamer talking to a fixed camera looks the same all
night. So two clips only count as the same footage when their sound matches too: the
per-second loudness of one must line up closely with the other at some small shift.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from engine.media.ffmpeg import ffmpeg_path, no_window_kwargs

_W, _H = 9, 8
FRAME_EVERY_S = 4.0
MATCH_BITS = 10  # two frames match when at most this many of 64 bits differ
MATCH_SHARE = 0.5  # two clips match when this share of the shorter one's frames match


def frame_hashes(
    source: Path, input_args: list[str], start: float, duration: float, cwd: Path | None = None
) -> list[int]:
    """One hash per FRAME_EVERY_S seconds of [start, start + duration]."""
    try:
        done = subprocess.run(
            [
                ffmpeg_path(),
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                *input_args,
                "-ss",
                f"{start:.3f}",
                "-t",
                f"{duration:.3f}",
                "-i",
                str(source),
                "-an",
                "-vf",
                f"fps=1/{FRAME_EVERY_S},scale={_W}:{_H}:flags=area,format=gray",
                "-f",
                "rawvideo",
                "pipe:1",
            ],  # fmt: skip
            capture_output=True,
            timeout=120,
            cwd=cwd,
            **no_window_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    data = done.stdout
    size = _W * _H
    return [_dhash(data[i : i + size]) for i in range(0, len(data) - size + 1, size)]


def _dhash(pixels: bytes) -> int:
    value = 0
    for row in range(_H):
        for col in range(_W - 1):
            left = pixels[row * _W + col]
            right = pixels[row * _W + col + 1]
            value = (value << 1) | (1 if left > right else 0)
    return value


def encode(hashes: list[int]) -> str:
    return "".join(f"{value:016x}" for value in hashes)


def decode(text: str | None) -> list[int]:
    if not text or len(text) % 16:
        return []
    try:
        return [int(text[i : i + 16], 16) for i in range(0, len(text), 16)]
    except ValueError:
        return []


def similarity(a: list[int], b: list[int]) -> float:
    """Share of the shorter clip's frames that have a near-identical frame in the other."""
    if not a or not b:
        return 0.0
    # A flat frame (black screen, fade) hashes to all zeros and would match any other.
    a = [frame for frame in a if frame]
    b = [frame for frame in b if frame]
    if not a or not b:
        return 0.0
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    hits = sum(
        1 for frame in short if any((frame ^ other).bit_count() <= MATCH_BITS for other in long)
    )
    return hits / len(short)


def is_duplicate(a: list[int], b: list[int]) -> bool:
    return similarity(a, b) >= MATCH_SHARE


AUDIO_MAX_SHIFT_S = 12
AUDIO_MIN_OVERLAP_S = 10
AUDIO_MIN_CORRELATION = 0.9
_AUDIO_MIN_SPREAD_DB = 1.0  # flatter than this and the shape of the sound proves nothing


def audio_matches(a: list[float], b: list[float]) -> bool:
    """Do two per-second loudness curves show the same sound, allowing a small shift?"""
    for shift in range(-AUDIO_MAX_SHIFT_S, AUDIO_MAX_SHIFT_S + 1):
        xs = a[max(0, shift) :]
        ys = b[max(0, -shift) :]
        n = min(len(xs), len(ys))
        if n >= AUDIO_MIN_OVERLAP_S and _correlation(xs[:n], ys[:n]) >= AUDIO_MIN_CORRELATION:
            return True
    return False


def _correlation(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    mean_x, mean_y = sum(xs) / n, sum(ys) / n
    var_x = sum((x - mean_x) ** 2 for x in xs) / n
    var_y = sum((y - mean_y) ** 2 for y in ys) / n
    if var_x**0.5 < _AUDIO_MIN_SPREAD_DB or var_y**0.5 < _AUDIO_MIN_SPREAD_DB:
        return 0.0
    covariance = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / n
    return covariance / (var_x * var_y) ** 0.5


def same_footage(
    frames_a: list[int], audio_a: list[float], frames_b: list[int], audio_b: list[float]
) -> bool:
    return is_duplicate(frames_a, frames_b) and audio_matches(audio_a, audio_b)
