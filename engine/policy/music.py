"""Is there music in a clip's audio?

This detects the *presence* of music, not whose music it is: naming a copyrighted track
needs a fingerprint service. Presence is the cautious stand-in. Game soundtracks, radio
stations inside games and background playlists are what draw copyright claims, so a
clip with music in it is never auto-published; a person decides.

How: music holds notes. Its spectrum has peaks that stay at the same frequency for a
third of a second or more, over and over, and usually a steady beat. Speech glides in
pitch and has no steady beat; game sound effects are noisy and brief. Two measurements:
the share of sounding frames with a held spectral peak, and how periodic the onsets are.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from engine.media.ffmpeg import ffmpeg_path, no_window_kwargs

RATE = 8000
_WINDOW = 1024  # 128 ms: about 8 Hz resolution, enough to tell a held note from a glide
_HOP = 256  # 32 ms
_HOLD_FRAMES = 10  # a peak must stay put this long (320 ms) to count as a held note
_BAND = (100.0, 2000.0)


@dataclass(frozen=True)
class MusicReading:
    held_notes: float  # share of sounding frames with a held spectral peak, 0..1
    beat: float  # strength of the steadiest tempo between 60 and 180 BPM, 0..1
    likelihood: float  # combined, 0..1
    seconds: float

    def as_dict(self) -> dict:
        return {
            "held_notes": round(self.held_notes, 3),
            "beat": round(self.beat, 3),
            "likelihood": round(self.likelihood, 3),
        }


def decode_mono(path: Path, timeout_s: float = 60.0) -> np.ndarray:
    """The file's audio as float32 samples at 8 kHz mono. Empty if there is none."""
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
                str(RATE),
                "-f",
                "s16le",
                "pipe:1",
            ],  # fmt: skip
            capture_output=True,
            timeout=timeout_s,
            **no_window_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return np.zeros(0, dtype=np.float32)
    raw = done.stdout[: len(done.stdout) // 2 * 2]
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0


def analyse(samples: np.ndarray) -> MusicReading:
    seconds = len(samples) / RATE
    if len(samples) < RATE * 2:
        return MusicReading(0.0, 0.0, 0.0, seconds)

    frames = 1 + (len(samples) - _WINDOW) // _HOP
    index = np.arange(_WINDOW)[None, :] + _HOP * np.arange(frames)[:, None]
    spectrum = np.abs(np.fft.rfft(samples[index] * np.hanning(_WINDOW), axis=1))
    freqs = np.fft.rfftfreq(_WINDOW, 1 / RATE)
    band = spectrum[:, (freqs >= _BAND[0]) & (freqs <= _BAND[1])]

    energy = band.sum(axis=1)
    sounding = energy > max(energy.max() * 0.02, 1e-6)
    if sounding.sum() < 20:
        return MusicReading(0.0, 0.0, 0.0, seconds)

    # Peaks: local maxima that stand well clear of the frame's own noise floor.
    floor = np.median(band, axis=1, keepdims=True) + 1e-9
    local_max = (band > np.roll(band, 1, axis=1)) & (band >= np.roll(band, -1, axis=1))
    peaks = local_max & (band > 6 * floor) & (band > 0.25 * band.max(axis=1, keepdims=True))
    # Allow a one-bin wobble when comparing a peak with the frames before it.
    near = peaks | np.roll(peaks, 1, axis=1) | np.roll(peaks, -1, axis=1)
    held = peaks.copy()
    for back in range(1, _HOLD_FRAMES):
        held[back:] &= near[:-back]
        held[:back] = False
    held_frame = held.any(axis=1)
    # A held note covers the frames it was held through, not only its last one.
    covered = held_frame.copy()
    for back in range(1, _HOLD_FRAMES):
        covered[:-back] |= held_frame[back:]
    held_notes = float((covered & sounding).sum() / sounding.sum())

    # Beat: how strongly the onset pattern repeats at some tempo from 60 to 180 BPM.
    flux = np.maximum(np.diff(np.log1p(band), axis=0), 0).sum(axis=1)
    flux = flux - flux.mean()
    beat = 0.0
    if flux.std() > 1e-9 and len(flux) > 200:
        corr = np.correlate(flux, flux, mode="full")[len(flux) - 1 :]
        corr = corr / corr[0]
        low, high = int(RATE / _HOP * 60 / 180), int(RATE / _HOP * 60 / 60)
        beat = float(np.clip(corr[low : high + 1].max(), 0.0, 1.0))

    # Held notes are the evidence. A beat only adds to it: footsteps and gunfire are
    # periodic too, and synthetic or very flat voices hold a pitch for a while.
    likelihood = (held_notes - 0.3) / 0.3
    if held_notes > 0.3:
        likelihood += 0.2 * beat
    likelihood = float(np.clip(likelihood, 0.0, 1.0))
    return MusicReading(held_notes, beat, likelihood, seconds)


def detect_music(path: Path) -> MusicReading:
    return analyse(decode_mono(path))


if __name__ == "__main__":  # python -m engine.policy.music clip.mp4 [more files]
    import sys

    for name in sys.argv[1:]:
        reading = detect_music(Path(name))
        print(f"{name}: {reading.as_dict()} over {reading.seconds:.0f} s")
