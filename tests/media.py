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


def make_buffer(
    store,
    session_id: int,
    seconds: int,
    end_time: float,
    size: str = "1280x720",
    fps: int = 30,
    source: str = "testsrc2",
    beep_at: float | None = None,
    segment_seconds: int = 10,
    run: str = "100",
) -> None:
    """Fill a session's buffer with `seconds` of generated footage ending at `end_time`,
    exactly as the recorder would have left it (segments + ffmpeg's list, clock-aligned).
    `beep_at` puts one second of loud tone at that many seconds in; the rest is quiet."""
    import os

    folder = store.session_dir(session_id)
    folder.mkdir(parents=True, exist_ok=True)
    if beep_at is None:
        audio = "sine=frequency=330:sample_rate=48000,volume=0.05"
    else:
        audio = (
            "sine=frequency=880:sample_rate=48000,"
            f"volume='if(between(t,{beep_at},{beep_at + 1}),1.0,0.01)':eval=frame"
        )
    subprocess.run(
        [
            ffmpeg_path(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"{source}=size={size}:rate={fps}",
            "-f",
            "lavfi",
            "-i",
            audio,
            "-t",
            str(seconds),
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-g",
            str(fps),
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            "-f",
            "segment",
            "-segment_time",
            str(segment_seconds),
            "-segment_format",
            "mpegts",
            "-reset_timestamps",
            "1",
            "-segment_list",
            str(folder / f"{run}.csv"),
            "-segment_list_type",
            "csv",
            str(folder / f"{run}_%06d.ts"),
        ],  # fmt: skip
        check=True,
    )
    for line in (folder / f"{run}.csv").read_text().splitlines():
        name, _start, c_end = line.rsplit(",", 2)
        stamp = end_time - seconds + float(c_end)
        os.utime(folder / name, (stamp, stamp))
