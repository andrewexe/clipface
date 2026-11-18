"""Keeping the computer awake while a live stream is being recorded.

When Windows sleeps, recording stops, and on waking every connection fails until the
network is back ("could not reach TikTok (ConnectError)"). While at least one stream is
recording, the recorder tells Windows the system is in use. This resets the idle-sleep
timer the way a video player does. It never changes power settings, lets the screen turn
off, and stops as soon as nothing is recording, so the computer then sleeps as usual.
"""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Callable

log = logging.getLogger(__name__)

ES_SYSTEM_REQUIRED = 0x00000001
NUDGE_EVERY_S = 60.0  # well under the shortest idle-sleep time Windows offers (1 minute+)


def _windows_nudge() -> bool:
    import ctypes

    return bool(ctypes.windll.kernel32.SetThreadExecutionState(ES_SYSTEM_REQUIRED))


def default_nudge() -> Callable[[], bool] | None:
    return _windows_nudge if sys.platform == "win32" else None


_PLATFORM = object()  # use this platform's power API, if it has one


class KeepAwake:
    """Call `tick(recording)` often (the recorder does, every few seconds)."""

    def __init__(
        self,
        nudge: Callable[[], bool] | None | object = _PLATFORM,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._nudge = default_nudge() if nudge is _PLATFORM else nudge
        self._clock = clock
        self._last = float("-inf")
        self._warned = False

    def tick(self, recording: bool) -> None:
        if not recording or self._nudge is None:
            return
        now = self._clock()
        if now - self._last < NUDGE_EVERY_S:
            return
        self._last = now
        reason = "the power API refused"
        try:
            ok = bool(self._nudge())  # type: ignore[operator]
        except Exception as exc:  # noqa: BLE001 - never let this stop the recorder
            ok, reason = False, str(exc)
        if not ok and not self._warned:
            self._warned = True
            log.warning("could not keep the computer awake while recording: %s", reason)
