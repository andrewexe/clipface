"""Higgsfield plugin (optional): intros, transitions and thumbnails.

Not connected. The plan says to confirm that Higgsfield offers API access before
building on it, and that has not been confirmed: its developer documentation was not
reachable from where this was built, and nothing here guesses at an API. The plugin
therefore reports that it is not ready and does nothing. When API access is confirmed,
this is the one file to fill in; the rest of the app already treats it as optional.
"""

from __future__ import annotations

from pathlib import Path


class HiggsfieldPlugin:
    name = "higgsfield"

    def status(self) -> dict:
        return {
            "ready": False,
            "reason": "Not connected: Higgsfield API access has not been confirmed.",
        }

    def thumbnail(self, clip_video: Path, title: str) -> Path | None:
        return None

    def intro(self, clip_video: Path, title: str) -> Path | None:
        return None
