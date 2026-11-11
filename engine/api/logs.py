"""Read-only views for the Logs page: the audit trail and the engine log tail."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select

from engine.api.app import AppContext, get_ctx
from engine.db.models import AuditLog

router = APIRouter()

_TAIL_BYTES = 512_000


@router.get("/audit")
def audit(limit: int = Query(100, ge=1, le=500), ctx: AppContext = Depends(get_ctx)) -> dict:
    with ctx.session_factory() as session:
        rows = session.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)).all()
    return {
        "entries": [
            {
                "id": row.id,
                "ts": row.ts.isoformat(),
                "actor": row.actor,
                "action": row.action,
                "object_id": row.object_id,
                "details": row.details,
            }
            for row in rows
        ]
    }


@router.get("/logs")
def logs(lines: int = Query(200, ge=1, le=1000), ctx: AppContext = Depends(get_ctx)) -> dict:
    """The log file is already redacted when written; this only reads its tail."""
    path = ctx.config.log_file
    if not path.exists():
        return {"lines": []}
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - _TAIL_BYTES))
        chunk = handle.read().decode("utf-8", errors="replace")
    tail = chunk.splitlines()
    if size > _TAIL_BYTES:
        tail = tail[1:]  # first line is probably cut mid-way
    return {"lines": tail[-lines:]}
