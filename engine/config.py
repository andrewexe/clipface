"""Engine configuration: where data, the database, and logs live."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import platformdirs

APP_NAME = "Clipper"


@dataclass(frozen=True)
class Config:
    data_dir: Path

    @property
    def db_path(self) -> Path:
        return self.data_dir / "clipper.db"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def log_file(self) -> Path:
        return self.log_dir / "engine.log"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)


def load_config() -> Config:
    """CLIPPER_DATA_DIR overrides the per-user default (%LOCALAPPDATA%\\Clipper on Windows)."""
    override = os.environ.get("CLIPPER_DATA_DIR")
    if override:
        return Config(data_dir=Path(override))
    return Config(data_dir=Path(platformdirs.user_data_dir(APP_NAME, appauthor=False)))
