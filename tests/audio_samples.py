"""Generated audio for policy tests: music-like, speech (if a synthesizer is installed),
and noise. Nothing here is real music or a real voice."""

from __future__ import annotations

import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np

from engine.media.ffmpeg import ffmpeg_path

RATE = 8000


def song(seconds: float = 20, bpm: int = 120, seed: int = 0, drums: bool = True) -> np.ndarray:
    """A random two-voice tune with harmonics and, optionally, a drum hit on each beat."""
    rng = np.random.default_rng(seed)
    beat = 60 / bpm
    out = np.zeros(int(RATE * seconds))
    scale = [220, 247, 262, 294, 330, 349, 392, 440]
    for i in range(int(seconds / beat)):
        start = int(i * beat * RATE)
        for freq in (scale[rng.integers(8)], scale[rng.integers(8)] * 1.5):
            t = np.arange(int(RATE * beat * rng.choice([1, 2]))) / RATE
            env = np.minimum(1, t / 0.01) * np.exp(-t * 1.5)
            tone = sum(
                a * np.sin(2 * np.pi * freq * (h + 1) * t) for h, a in enumerate((1, 0.5, 0.3, 0.2))
            )
            end = min(len(out), start + len(t))
            out[start:end] += (tone * env)[: end - start] * 0.3
        if drums:
            n = int(RATE * 0.05)
            hit = rng.normal(0, 1, n) * np.exp(-np.arange(n) / (RATE * 0.012))
            out[start : start + n] += hit[: len(out) - start] * 0.3
    return out.astype(np.float32)


def noise(seconds: float = 20, seed: int = 1) -> np.ndarray:
    """Bursty broadband noise, like footsteps or gunfire: periodic but not musical."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(RATE * seconds)) / RATE
    return (rng.normal(0, 0.2, len(t)) * (0.55 + 0.45 * np.sin(2 * np.pi * 3 * t))).astype(
        np.float32
    )


def speech(path: Path, text: str, voice: str = "en", speed: int = 165) -> np.ndarray | None:
    """Synthesized speech at 8 kHz, or None when espeak-ng is not installed."""
    if not shutil.which("espeak-ng"):
        return None
    raw = path.with_suffix(".raw.wav")
    subprocess.run(["espeak-ng", "-v", voice, "-s", str(speed), "-w", str(raw), text], check=True)
    subprocess.run(
        [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(raw),
         "-ac", "1", "-ar", str(RATE), str(path)],
        check=True,
    )  # fmt: skip
    with wave.open(str(path)) as handle:
        data = np.frombuffer(handle.readframes(handle.getnframes()), dtype="<i2")
    return data.astype(np.float32) / 32768.0


def write_wav(path: Path, samples: np.ndarray) -> Path:
    clipped = np.clip(samples, -1, 1)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes((clipped * 32767).astype("<i2").tobytes())
    return path


def clip_with_audio(path: Path, samples: np.ndarray, text: str | None = None) -> Path:
    """A small vertical MP4 with the given sound, and optionally text drawn on screen."""
    wav = write_wav(path.with_suffix(".wav"), samples)
    seconds = len(samples) / RATE
    video = "testsrc2=size=270x480:rate=10"
    if text:
        safe = text.replace(":", r"\:").replace("'", "")
        video += (
            f",drawtext=text='{safe}':fontsize=18:fontcolor=white:box=1:boxcolor=black:x=10:y=200"
        )
    subprocess.run(
        [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", video,
         "-i", str(wav), "-t", f"{seconds:.2f}", "-c:v", "libx264", "-preset", "ultrafast",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "64k", str(path)],
        check=True,
    )  # fmt: skip
    return path
