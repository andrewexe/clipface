"""Non-secret app settings, stored as JSON in the data folder. Secrets never go here."""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

log = logging.getLogger(__name__)


class AppSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # Recording
    buffer_minutes: int = Field(20, ge=2, le=120)
    segment_seconds: int = Field(10, ge=2, le=60)
    disk_cap_gb: float = Field(50.0, ge=1, le=4000)
    raw_retention_hours: float = Field(6.0, ge=0.5, le=72)
    stream_quality: str = Field("best", pattern=r"^[a-z0-9_,]{1,64}$")


class SettingsStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._current = self._load()

    def _load(self) -> AppSettings:
        if not self._path.exists():
            return AppSettings()
        try:
            return AppSettings.model_validate(json.loads(self._path.read_text(encoding="utf-8")))
        except (OSError, ValueError, ValidationError) as exc:
            # A damaged file must not stop the app; fall back to safe defaults.
            log.warning("settings file unreadable (%s); using defaults", type(exc).__name__)
            return AppSettings()

    def get(self) -> AppSettings:
        with self._lock:
            return self._current.model_copy()

    def update(self, patch: dict[str, Any]) -> tuple[AppSettings, dict[str, dict[str, Any]]]:
        """Validate and save. Returns the new settings and {field: {from, to}} for the audit."""
        with self._lock:
            merged = AppSettings.model_validate({**self._current.model_dump(), **patch})
            before, after = self._current.model_dump(), merged.model_dump()
            changes = {
                key: {"from": before[key], "to": after[key]}
                for key in after
                if before[key] != after[key]
            }
            if changes:
                tmp = self._path.with_suffix(".tmp")
                tmp.write_text(json.dumps(after, indent=2), encoding="utf-8")
                tmp.replace(self._path)
                self._current = merged
            return merged.model_copy(), changes
