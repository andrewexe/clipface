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
from engine.db.session import create_db_engine, run_migrations
from engine.keychain import SecretStore
from engine.logging_setup import SecretRedactor, setup_logging

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
    app = create_app(AppContext(config=config, token=token, db_engine=db_engine, secrets=secrets))

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
            lifespan="off",
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
