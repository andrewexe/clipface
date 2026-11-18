"""How much disk the app uses, and a button to clean up now."""

from __future__ import annotations

import time
from pathlib import Path

from fastapi import APIRouter, Depends

from engine import storage
from engine.api.app import AppContext, get_ctx

router = APIRouter(prefix="/storage")


def _export_dir(ctx: AppContext) -> Path:
    custom = ctx.settings.get().export_dir.strip()
    return Path(custom).expanduser() if custom else ctx.config.default_export_dir


@router.get("")
def usage(ctx: AppContext = Depends(get_ctx)) -> dict:
    config = ctx.config
    parts = {
        "buffer": storage.folder_size(config.buffer_dir),
        "raw": storage.folder_size(config.raw_dir),
        "clips": storage.folder_size(config.clips_dir),
        "logs": storage.folder_size(config.log_dir),
        "database": sum(
            p.stat().st_size for p in config.data_dir.glob("clipper.db*") if p.is_file()
        ),
    }
    return {
        "bytes": parts,
        "total_bytes": sum(parts.values()),
        "export_bytes": storage.folder_size(_export_dir(ctx)),
        "export_dir": str(_export_dir(ctx)),
        "buffer_cap_bytes": int(ctx.settings.get().disk_cap_gb * 1024**3),
    }


@router.post("/cleanup")
def cleanup(ctx: AppContext = Depends(get_ctx)) -> dict:
    retention = ctx.settings.get().raw_retention_hours * 3600
    freed = storage.sweep(ctx.session_factory, ctx.config, time.time(), retention)
    if ctx.recorder is not None:
        ctx.recorder.wake()
    return {"deleted_files": freed.files, "freed_bytes": freed.bytes, **usage(ctx)}
