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

    # Highlight detection
    detect_z_threshold: float = Field(3.0, ge=1.5, le=12)
    detect_cooldown_s: int = Field(60, ge=10, le=600)
    detect_audio: bool = True
    detect_audio_z: float = Field(4.0, ge=2, le=12)
    detect_clips_count: int = Field(3, ge=0, le=50)  # viewer clips in 90 s; 0 turns it off

    # Clips
    clip_before_s: int = Field(30, ge=5, le=120)
    clip_after_s: int = Field(10, ge=2, le=60)
    captions: bool = True
    transcription_usd_per_minute: float = Field(0.01, ge=0, le=1)
    transcription_daily_cap_usd: float = Field(2.0, ge=0, le=1000)

    # Claude scoring
    scoring: bool = True
    scoring_model: str = Field("claude-opus-5-5", pattern=r"^claude-[a-z0-9-]{3,60}$")
    score_min: float = Field(0.4, ge=0, le=1)  # moments scored below this are not rendered
    anthropic_daily_cap_usd: float = Field(5.0, ge=0, le=1000)

    # Policy gate and autonomy
    autonomy_level: str = Field("L1", pattern=r"^L[12]$")  # L1: every clip needs approval
    autonomy_l2_since: str = ""  # when L2 was last switched on (ISO time)
    publishing_paused: bool = False  # the kill switch
    auto_approve_score: float = Field(0.8, ge=0.5, le=1)
    review_sample_rate: float = Field(0.10, ge=0, le=1)
    music_threshold: float = Field(0.5, ge=0.05, le=1)
    music_action: str = Field("review", pattern=r"^(review|reject)$")
    blocked_words: list[str] = Field(default_factory=list, max_length=200)

    # Publishing
    youtube_privacy: str = Field("private", pattern=r"^(private|unlisted|public)$")
    post_gap_min_minutes: int = Field(20, ge=1, le=720)
    post_gap_max_minutes: int = Field(90, ge=1, le=1440)
    post_window_start_hour: int = Field(9, ge=0, le=23)
    post_window_end_hour: int = Field(23, ge=1, le=24)

    # Phone approvals
    telegram_chat_id: int = 0  # the one private chat the bot obeys; 0 means not paired
    telegram_notify: bool = True

    # Review and export
    export_dir: str = Field("", max_length=500)  # empty: the default folder in Videos


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
