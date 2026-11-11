"""Development-only engine with scripted fake platforms, for working on the UI without
real Twitch/Kick keys. Never shipped; never used by the app.

    uv run python -m tests.dev_engine            # then, in app/:
    VITE_ENGINE_URL=http://127.0.0.1:8765 VITE_ENGINE_TOKEN=<printed token> pnpm dev

Script: twitch "alwayslive" is live from the start; twitch "golive" goes live 15 seconds
after it is added; kick "kicklive" is live; anything containing "ghost" does not exist.
"""

from __future__ import annotations

import os
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import keyring
import uvicorn

from engine.api.app import AppContext, create_app
from engine.config import Config
from engine.db.session import create_db_engine, run_migrations
from engine.keychain import SecretStore
from engine.logging_setup import SecretRedactor, setup_logging
from engine.monitor.platforms import LiveInfo, MissingCredentials, Snapshot
from engine.monitor.service import Monitor
from tests.fake_keyring import MemoryKeyring

TOKEN = "dev-" + "0" * 60
PORT = 8765


class ScriptedPlatform:
    def __init__(self, platform: str, need_keys: bool = False) -> None:
        self.platform = platform
        self.need_keys = need_keys
        self.first_seen: dict[str, float] = {}
        self.started: dict[str, datetime] = {}

    async def check(self, usernames, verify):
        if self.need_keys:
            raise MissingCredentials(f"{self.platform} keys are not set")
        snap = Snapshot()
        for name in usernames:
            first = self.first_seen.setdefault(name, time.monotonic())
            if "ghost" in name:
                if name in verify:
                    snap.missing.add(name)
                continue
            if name in verify:
                snap.found[name] = name.title()
            is_live = "live" in name and ("golive" not in name or time.monotonic() - first >= 15)
            if is_live:
                started = self.started.setdefault(name, datetime.now(UTC).replace(microsecond=0))
                snap.live[name] = LiveInfo(
                    started_at=started,
                    title="GTA VI launch day - first look",
                    game="Grand Theft Auto VI",
                    viewer_count=48213,
                )
        return snap

    async def aclose(self) -> None:
        pass


def main() -> None:
    keyring.set_keyring(MemoryKeyring())
    data_dir = Path(os.environ.get("CLIPPER_DATA_DIR") or tempfile.mkdtemp(prefix="clipper-dev-"))
    config = Config(data_dir=data_dir)
    config.ensure_dirs()
    redactor = SecretRedactor()
    setup_logging(config, redactor)
    db_engine = create_db_engine(config.db_path)
    run_migrations(db_engine)
    ctx = AppContext(config=config, token=TOKEN, db_engine=db_engine, secrets=SecretStore(redactor))
    ctx.monitor = Monitor(
        ctx.session_factory,
        {
            "twitch": ScriptedPlatform("twitch"),
            "kick": ScriptedPlatform("kick", need_keys=bool(os.environ.get("DEV_KICK_NO_KEYS"))),
        },
        poll_interval_s=float(os.environ.get("DEV_POLL_S", "20")),
    )
    print(f"dev engine on http://127.0.0.1:{PORT}  token: {TOKEN}", flush=True)
    uvicorn.run(create_app(ctx), host="127.0.0.1", port=PORT, log_config=None, access_log=False)


if __name__ == "__main__":
    main()
