"""Test helpers that make real video with ffmpeg: a fake live stream and sample files."""

from __future__ import annotations

import subprocess

from engine.media.ffmpeg import ffmpeg_path
from engine.recorder.service import StreamUnavailable


class TestPatternSource:
    """Stands in for Streamlink: a live MPEG-TS stream (test card + tone) in real time."""

    __test__ = False

    def __init__(self, seconds: float | None = None, size: str = "320x180", bitrate: str = "300k"):
        self.seconds = seconds
        self.size = size
        self.bitrate = bitrate
        self.opened: list[str] = []
        self.unavailable = False

    def open(self, url: str, quality: str):
        self.opened.append(url)
        if self.unavailable:
            raise StreamUnavailable("offline")
        duration = ["-t", str(self.seconds)] if self.seconds else []
        process = subprocess.Popen(
            [
                ffmpeg_path(),
                "-hide_banner",
                "-loglevel",
                "error",
                "-re",
                "-f",
                "lavfi",
                "-i",
                f"testsrc2=size={self.size}:rate=30",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000",
                *duration,
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-tune",
                "zerolatency",
                "-b:v",
                self.bitrate,
                "-g",
                "30",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "64k",
                "-f",
                "mpegts",
                "pipe:1",
            ],  # fmt: skip
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        return _Handle(process)


class _Handle:
    def __init__(self, process: subprocess.Popen):
        self._process = process

    def read(self, size: int) -> bytes:
        return self._process.stdout.read1(size)

    def close(self) -> None:
        if self._process.poll() is None:
            self._process.kill()
        self._process.wait()


def probe(path) -> dict:
    """Duration and stream kinds of a media file, read from ffmpeg's own report."""
    import re

    out = subprocess.run(
        [ffmpeg_path(), "-hide_banner", "-i", str(path)], capture_output=True, text=True
    ).stderr
    duration = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", out)
    seconds = None
    if duration:
        h, m, s = duration.groups()
        seconds = int(h) * 3600 + int(m) * 60 + float(s)
    size = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", out)
    return {
        "duration": seconds,
        "video": "Video:" in out,
        "audio": "Audio:" in out,
        "width": int(size.group(1)) if size else None,
        "height": int(size.group(2)) if size else None,
    }
