"""`clipper-engine --self-check`: does this build have everything it needs?

Prints one JSON object and exits 0 if every check passed. It makes no network calls and
reads no secrets. Useful after packaging, and on a new machine.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path


def run_checks() -> dict[str, dict]:
    results: dict[str, dict] = {}

    def check(name: str, fn) -> None:
        try:
            results[name] = {"ok": True, "detail": fn()}
        except Exception as exc:  # noqa: BLE001 - every failure is reported, none raised
            results[name] = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"[:200]}

    def ffmpeg() -> str:
        from engine.media.ffmpeg import ffmpeg_path, no_window_kwargs

        path = ffmpeg_path()
        done = subprocess.run(
            [path, "-hide_banner", "-filters"], capture_output=True, text=True, **no_window_kwargs()
        )
        missing = [
            f for f in ("ass", "loudnorm", "boxblur", "vstack") if f" {f} " not in done.stdout
        ]
        if missing:
            raise RuntimeError(f"ffmpeg lacks filters: {missing}")
        encoders = subprocess.run(
            [path, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            **no_window_kwargs(),
        ).stdout
        if "libx264" not in encoders:
            raise RuntimeError("ffmpeg lacks the libx264 encoder")
        return Path(path).name

    def stream_plugins() -> str:
        from streamlink import Streamlink

        session = Streamlink()
        names = [
            session.resolve_url(url)[0]
            for url in ("https://www.twitch.tv/example", "https://kick.com/example")
        ]
        if names != ["twitch", "kick"]:
            raise RuntimeError(f"resolved {names}")
        return ", ".join(names)

    def database() -> str:
        from engine.db.session import create_db_engine, run_migrations

        with tempfile.TemporaryDirectory() as folder:
            db_engine = create_db_engine(Path(folder) / "check.db")
            try:
                return f"schema {run_migrations(db_engine)}"
            finally:
                db_engine.dispose()

    def blocklist() -> str:
        from engine.policy.text import _built_in, blocked_word_count

        normal, stretched = _built_in()
        if not normal or blocked_word_count("clipperblocklisttest") != 1:
            raise RuntimeError("the built-in blocked-word list did not load")
        return f"{len(normal)} entries"

    def music_detector() -> str:
        import numpy as np

        from engine.policy.music import analyse

        t = np.arange(8000 * 4) / 8000
        tone = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
        return f"held tone likelihood {analyse(tone).likelihood:.2f}"

    def keychain() -> str:
        from engine.keychain import SecretStore
        from engine.logging_setup import SecretRedactor

        backend = SecretStore(SecretRedactor()).backend()
        if not backend["available"]:
            raise RuntimeError(f"no usable OS keychain ({backend['name']})")
        return str(backend["name"])

    def claude_sdk() -> str:
        import anthropic

        return f"anthropic {anthropic.__version__}"

    def mcp_server() -> str:
        from engine.mcp_server import build_server

        build_server(lambda method, path: {})
        return "5 tools"

    def api() -> str:
        from engine.api import app as api_app

        return f"{len(api_app.ALLOWED_ORIGINS)} allowed origins"

    for name, fn in (
        ("ffmpeg", ffmpeg),
        ("stream_plugins", stream_plugins),
        ("database", database),
        ("blocklist", blocklist),
        ("music_detector", music_detector),
        ("keychain", keychain),
        ("claude_sdk", claude_sdk),
        ("mcp_server", mcp_server),
        ("api", api),
    ):
        check(name, fn)
    return results


def main() -> int:
    results = run_checks()
    print(json.dumps(results, indent=2))
    return 0 if all(item["ok"] for item in results.values()) else 1
