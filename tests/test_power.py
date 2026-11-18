"""Keeping the computer awake while recording."""

from __future__ import annotations

import logging

from engine.power import NUDGE_EVERY_S, KeepAwake


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_nudges_at_most_once_a_minute_and_only_while_recording():
    calls, clock = [], Clock()
    awake = KeepAwake(nudge=lambda: calls.append(clock.now) or True, clock=clock)
    awake.tick(recording=False)
    assert calls == []
    awake.tick(recording=True)
    clock.now += 5
    awake.tick(recording=True)  # the recorder checks every 5 s: still one call
    assert calls == [1000.0]
    clock.now += NUDGE_EVERY_S
    awake.tick(recording=True)
    assert len(calls) == 2


def test_a_failing_nudge_warns_once_and_never_raises(caplog):
    clock = Clock()

    def broken() -> bool:
        raise OSError("no power API")

    awake = KeepAwake(nudge=broken, clock=clock)
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            awake.tick(recording=True)
            clock.now += NUDGE_EVERY_S
    assert sum("awake" in r.message for r in caplog.records) == 1


def test_does_nothing_where_there_is_no_power_api():
    KeepAwake(nudge=None, clock=Clock()).tick(recording=True)  # no error
