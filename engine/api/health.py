from __future__ import annotations

import time

from fastapi import APIRouter, Depends
from sqlalchemy import text

from engine import __version__
from engine.api.app import AppContext, get_ctx

router = APIRouter()


@router.get("/health")
def health(ctx: AppContext = Depends(get_ctx)) -> dict:
    try:
        with ctx.db_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        db = {"ok": True, "revision": ctx.schema_revision}
    except Exception as exc:  # noqa: BLE001 - health must report, not raise
        db = {"ok": False, "error": type(exc).__name__}
    return {
        "status": "ok" if db["ok"] else "degraded",
        "version": __version__,
        "db": db,
        "keychain": ctx.secrets.backend(),
        "uptime_s": round(time.monotonic() - ctx.started_at, 1),
    }
