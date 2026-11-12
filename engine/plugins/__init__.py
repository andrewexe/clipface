"""Optional plugins that can dress a clip up (an intro, a transition, a thumbnail).

A plugin is off unless it says it is ready. The renderer does not depend on any of them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class ClipEnhancer(Protocol):
    name: str

    def status(self) -> dict:
        """{"ready": bool, "reason": str}: why it can or cannot be used right now."""

    def thumbnail(self, clip_video: Path, title: str) -> Path | None:
        """A custom thumbnail image for the clip, or None to keep the default."""

    def intro(self, clip_video: Path, title: str) -> Path | None:
        """A short intro video to put before the clip, or None for no intro."""
