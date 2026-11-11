"""Find ffmpeg: one on PATH if present, otherwise the copy bundled with the app."""

from __future__ import annotations

import functools
import shutil


class FfmpegNotFound(RuntimeError):
    pass


@functools.cache
def ffmpeg_path() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # noqa: BLE001 - any failure here means "no ffmpeg"
        raise FfmpegNotFound("ffmpeg was not found on this machine") from exc


def no_window_kwargs() -> dict:
    """Popen arguments that stop a console window flashing up on Windows."""
    import subprocess
    import sys

    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}
