"""Engine entry point: `python -m engine`.

The desktop app starts this as a child process and passes the session token in the
CLIPPER_SESSION_TOKEN environment variable. The engine binds a loopback port, then
prints one line to stdout so the app knows where to connect:

    CLIPPER_ENGINE_READY {"port": 51234, "version": "0.1.0"}

On a startup failure it prints `CLIPPER_ENGINE_ERROR <reason>` and exits non-zero.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
import threading

import uvicorn

from engine import __version__
from engine.api.app import AppContext, create_app
from engine.config import load_config
from engine.control.spend import SpendLedger
from engine.db.models import utcnow
from engine.db.session import create_db_engine, run_migrations
from engine.keychain import KeychainUnavailable, SecretStore
from engine.logging_setup import SecretRedactor, setup_logging
from engine.monitor.platforms import Credentials, KickClient, TwitchClient
from engine.monitor.service import Monitor
from engine.policy.classifier import PolicyClassifier
from engine.policy.gate import PolicyGate
from engine.publish.service import Publisher
from engine.publish.youtube import YouTubeClient, YouTubePublisher
from engine.recorder.service import Recorder
from engine.recorder.store import SegmentStore
from engine.render.pipeline import ClipPipeline
from engine.scorer.claude import ClaudeScorer
from engine.scorer.transcribe import DeepgramTranscriber
from engine.signals.service import SignalService

log = logging.getLogger("engine")

HOST = "127.0.0.1"  # never 0.0.0.0: the API must not be reachable from the network
MIN_TOKEN_LEN = 32


def _emit(line: str) -> None:
    print(line, flush=True)


def _exit_when_stdin_closes(server: uvicorn.Server) -> None:
    """The parent holds our stdin open. EOF means the app is gone: shut down cleanly."""

    def watch() -> None:
        try:
            while sys.stdin.buffer.read(4096):
                pass
        except (OSError, ValueError):
            pass
        log.info("stdin closed; shutting down")
        server.should_exit = True

    if sys.stdin is not None:
        threading.Thread(target=watch, name="stdin-watch", daemon=True).start()


def _credentials(secrets: SecretStore, id_name: str, secret_name: str):
    """Read a platform's keys from the keychain each time, so new keys apply at once."""

    def read() -> Credentials | None:
        try:
            client_id, client_secret = secrets.get(id_name), secrets.get(secret_name)
        except KeychainUnavailable:
            return None
        return (client_id, client_secret) if client_id and client_secret else None

    return read


def _secret(secrets: SecretStore, name: str):
    def read() -> str | None:
        try:
            return secrets.get(name)
        except KeychainUnavailable:
            return None

    return read


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="clipper-engine")
    parser.add_argument("--port", type=int, default=0, help="0 picks a free port (default)")
    parser.add_argument(
        "--exit-on-stdin-close",
        action="store_true",
        help="shut down when stdin reaches EOF (used by the desktop app)",
    )
    args = parser.parse_args(argv)

    # Popped so child processes (ffmpeg, streamlink) never inherit it.
    token = os.environ.pop("CLIPPER_SESSION_TOKEN", "")
    if len(token) < MIN_TOKEN_LEN:
        _emit("CLIPPER_ENGINE_ERROR CLIPPER_SESSION_TOKEN is missing or shorter than 32 chars")
        return 2

    config = load_config()
    config.ensure_dirs()
    redactor = SecretRedactor()
    redactor.register(token)
    setup_logging(config, redactor)

    try:
        db_engine = create_db_engine(config.db_path)
        revision = run_migrations(db_engine)
    except Exception:
        log.exception("database migration failed")
        _emit("CLIPPER_ENGINE_ERROR database migration failed; see the engine log")
        return 1

    secrets = SecretStore(redactor)
    secrets.warm()
    ctx = AppContext(config=config, token=token, db_engine=db_engine, secrets=secrets)
    twitch = TwitchClient(_credentials(secrets, "twitch_client_id", "twitch_client_secret"))
    kick = KickClient(_credentials(secrets, "kick_client_id", "kick_client_secret"))
    ctx.monitor = Monitor(ctx.session_factory, {"twitch": twitch, "kick": kick})
    store = SegmentStore(config.buffer_dir, ctx.settings.get().segment_seconds)
    ctx.recorder = Recorder(ctx.session_factory, store, ctx.settings)
    ctx.monitor.add_listener(ctx.recorder.on_session_event)
    ctx.signals = SignalService(
        ctx.session_factory, ctx.settings, store, monitor=ctx.monitor, clips=twitch
    )
    ledger = SpendLedger(ctx.session_factory)
    ctx.pipeline = ClipPipeline(
        ctx.session_factory,
        config,
        ctx.settings,
        store,
        ledger,
        transcriber=DeepgramTranscriber(_secret(secrets, "transcription_api_key")),
        scorer=ClaudeScorer(_secret(secrets, "anthropic_api_key"), ctx.settings, ledger),
        is_recording=ctx.recorder.is_recording,
    )
    ctx.signals.add_listener(ctx.pipeline.on_moment)
    ctx.gate = PolicyGate(
        ctx.session_factory,
        ctx.settings,
        classifier=PolicyClassifier(_secret(secrets, "anthropic_api_key"), ctx.settings, ledger),
    )
    ctx.pipeline.add_listener(ctx.gate.review)
    youtube = YouTubeClient(_credentials(secrets, "google_client_id", "google_client_secret"))
    ctx.publisher = Publisher(
        ctx.session_factory,
        ctx.settings,
        secrets,
        {"youtube": YouTubePublisher(youtube, utcnow)},
    )
    ctx.gate.on_approved(ctx.publisher.schedule_clip)
    ctx.remove_published = ctx.publisher.remove_clip
    app = create_app(ctx)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((HOST, args.port))
    except OSError as exc:
        log.error("could not bind %s:%s: %s", HOST, args.port, exc)
        _emit(f"CLIPPER_ENGINE_ERROR could not bind port {args.port}")
        return 1
    sock.listen(128)
    port = sock.getsockname()[1]

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            log_config=None,
            access_log=False,
            lifespan="on",
            timeout_graceful_shutdown=3,
        )
    )
    if args.exit_on_stdin_close:
        _exit_when_stdin_closes(server)

    log.info(
        "engine %s listening on %s:%s (schema %s, keychain: %s)",
        __version__,
        HOST,
        port,
        revision,
        secrets.backend()["name"],
    )
    _emit("CLIPPER_ENGINE_READY " + json.dumps({"port": port, "version": __version__}))
    server.run(sockets=[sock])
    log.info("engine stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
