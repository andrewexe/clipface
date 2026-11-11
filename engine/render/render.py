"""Render one vertical clip from recorded segments with ffmpeg."""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from engine.media.ffmpeg import ffmpeg_path, no_window_kwargs
from engine.media.probe import probe
from engine.render.layout import build_filter

CONCAT_LIST = "segments.txt"
CONCAT_ARGS = ["-f", "concat", "-safe", "0"]
CAPTIONS_FILE = "captions.ass"
RENDER_TIMEOUT_S = 600


class RenderError(Exception):
    pass


@dataclass(frozen=True)
class RenderResult:
    path: Path
    duration: float
    seconds_taken: float
    captions: bool
    layout: str


def write_concat_list(work: Path, segment_names: list[str]) -> Path:
    """The segments must sit in `work`; names only, so no path quoting problems."""
    path = work / CONCAT_LIST
    path.write_text("".join(f"file '{name}'\n" for name in segment_names), encoding="utf-8")
    return path


def _run(args: list[str], cwd: Path, timeout: float) -> None:
    try:
        done = subprocess.run(
            [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            **no_window_kwargs(),
        )
    except subprocess.TimeoutExpired:
        raise RenderError("ffmpeg took too long") from None
    except OSError as exc:
        raise RenderError(f"could not run ffmpeg ({type(exc).__name__})") from None
    if done.returncode != 0:
        tail = (done.stderr or "").strip().splitlines()[-1:] or ["unknown error"]
        raise RenderError(f"ffmpeg failed: {tail[0][:200]}")


def render_clip(
    *,
    work: Path,
    segment_names: list[str],
    offset: float,
    duration: float,
    out: Path,
    layout: dict | None = None,
    ass_text: str | None = None,
) -> RenderResult:
    """Cut [offset, offset + duration] out of the joined segments and render it as a
    1080x1920 H.264/AAC MP4 with normalised loudness and optional burned-in captions."""
    started = time.monotonic()
    write_concat_list(work, segment_names)
    info = probe(work / segment_names[0])
    if not info.has_video or not info.width or not info.height:
        raise RenderError("the recording has no video")
    captions = None
    if ass_text:
        (work / CAPTIONS_FILE).write_text(ass_text, encoding="utf-8")
        captions = CAPTIONS_FILE
    graph = build_filter(info.width, info.height, layout, captions)
    out.parent.mkdir(parents=True, exist_ok=True)
    audio = (
        [
            "-map",
            "0:a:0",
            "-af",
            "loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
        ]  # fmt: skip
        if info.has_audio
        else ["-an"]
    )
    _run(
        [
            *CONCAT_ARGS,
            "-ss",
            f"{offset:.3f}",
            "-t",
            f"{duration:.3f}",
            "-i",
            CONCAT_LIST,
            "-filter_complex",
            graph,
            "-map",
            "[v]",
            *audio,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "21",
            "-pix_fmt",
            "yuv420p",
            "-fpsmax",
            "60",
            "-movflags",
            "+faststart",
            "-t",
            f"{duration:.3f}",
            str(out.resolve()),
        ],  # fmt: skip
        cwd=work,
        timeout=RENDER_TIMEOUT_S,
    )
    rendered = probe(out)
    if not rendered.has_video or not rendered.duration:
        raise RenderError("the rendered file is not playable")
    return RenderResult(
        path=out,
        duration=rendered.duration,
        seconds_taken=time.monotonic() - started,
        captions=captions is not None,
        layout="split" if "vstack" in graph else "blur",
    )


def make_thumbnail(video: Path, out: Path, at: float) -> bool:
    try:
        _run(
            [
                "-ss",
                f"{at:.2f}",
                "-i",
                str(video.resolve()),
                "-frames:v",
                "1",
                "-vf",
                "scale=360:-2",
                "-q:v",
                "4",
                str(out.resolve()),
            ],  # fmt: skip
            cwd=video.parent,
            timeout=60,
        )
    except RenderError:
        return False
    return out.exists()


def extract_audio(work: Path, offset: float, duration: float, out_name: str = "audio.flac") -> Path:
    """Mono 16 kHz FLAC of the clip window, for the transcription API."""
    _run(
        [
            *CONCAT_ARGS,
            "-ss",
            f"{offset:.3f}",
            "-t",
            f"{duration:.3f}",
            "-i",
            CONCAT_LIST,
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "flac",
            out_name,
        ],  # fmt: skip
        cwd=work,
        timeout=120,
    )
    return work / out_name
