"""Highlight detection: find the seconds where a signal jumps far above its own recent
normal.

Each signal is a per-second series (chat messages, hype messages, audio loudness). For
every second, the last `window_s` seconds are compared with the `baseline_s` seconds
before them, split into windows of the same size:

    z = (current window - median of baseline windows) / spread of baseline windows

The spread is the median absolute deviation (scaled to match a standard deviation), with
floors so that a quiet chat can't trigger on a handful of messages. Message counts are
compared on a square-root scale, which makes one threshold fair to all chat sizes.
Median and MAD are used instead of mean and standard deviation so that one highlight
does not raise the bar for the next five minutes.

This module is pure: no clock, no I/O. The same code runs live and on recorded logs.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field


@dataclass(frozen=True)
class SignalConfig:
    threshold: float = 3.0  # z-score needed to trigger
    kind: str = "count"  # "count": events per second, summed; "level": a gauge, averaged
    min_events: float = 8.0  # count signals: this many events in the window, at least
    min_scale: float = 1.0  # smallest spread used; for levels, in the signal's own unit


@dataclass(frozen=True)
class DetectorConfig:
    window_s: int = 10
    baseline_s: int = 300
    min_baseline_s: int = 90  # history needed before anything can trigger
    sustain_s: int = 3  # the signal must stay above its threshold this many seconds
    cooldown_s: int = 60  # per streamer: one moment per this many seconds
    peak_search_s: int = 20  # after a trigger, keep looking this long for the true peak
    signals: dict[str, SignalConfig] = field(
        default_factory=lambda: {
            "chat": SignalConfig(threshold=3.0, min_events=8),
            "hype": SignalConfig(threshold=3.0, min_events=6),
            "audio": SignalConfig(threshold=4.0, kind="level", min_scale=1.5),
        }
    )


class Series:
    """Sparse per-second values. Missing seconds count as zero for counts; for levels
    they are simply absent."""

    def __init__(self) -> None:
        self.values: dict[int, float] = {}
        self.first: int | None = None
        self.last: int | None = None

    def add(self, t: float, amount: float = 1.0) -> None:
        second = int(t)
        self.values[second] = self.values.get(second, 0.0) + amount
        self._touch(second)

    def set(self, t: float, value: float) -> None:
        second = int(t)
        self.values[second] = value
        self._touch(second)

    def _touch(self, second: int) -> None:
        self.first = second if self.first is None else min(self.first, second)
        self.last = second if self.last is None else max(self.last, second)

    def prune(self, before: int) -> None:
        for second in [s for s in self.values if s < before]:
            del self.values[second]

    def window(self, end: int, size: int, kind: str) -> float | None:
        """Aggregate of the `size` seconds ending at `end` (inclusive)."""
        got = [self.values[s] for s in range(end - size + 1, end + 1) if s in self.values]
        if kind == "count":
            return sum(got)
        return sum(got) / len(got) if len(got) >= size / 2 else None


@dataclass(frozen=True)
class Reading:
    z: float
    value: float
    baseline: float

    def as_dict(self) -> dict:
        return {
            "z": round(self.z, 2),
            "value": round(self.value, 2),
            "baseline": round(self.baseline, 2),
        }


def zscore(series: Series, t: int, cfg: DetectorConfig, signal: SignalConfig) -> Reading | None:
    """Score the window ending at second `t`. None means "not enough history yet"."""
    if series.first is None:
        return None
    current = series.window(t, cfg.window_s, signal.kind)
    if current is None:
        return None
    baseline: list[float] = []
    step = max(1, cfg.window_s // 2)  # half-overlapping windows: twice the samples
    end = t - cfg.window_s
    while end - cfg.window_s + 1 >= max(series.first, t - cfg.window_s - cfg.baseline_s + 1):
        value = series.window(end, cfg.window_s, signal.kind)
        if value is not None:
            baseline.append(value)
        end -= step
    if len(baseline) * step < cfg.min_baseline_s:
        return None
    median = statistics.median(baseline)
    if signal.kind == "count":
        # Message counts are Poisson-like: small numbers scatter far more, relative to
        # their size, than a bell curve allows. The Anscombe transform evens that out, so
        # the same threshold means the same rarity for a quiet chat and a busy one.
        stable = [_anscombe(value) for value in baseline]
        centre = statistics.median(stable)
        mad = statistics.median(abs(value - centre) for value in stable)
        scale = max(1.4826 * mad, 1.0, signal.min_scale)  # 1.0 = plain Poisson noise
        z = (_anscombe(current) - centre) / scale
    else:
        mad = statistics.median(abs(value - median) for value in baseline)
        z = (current - median) / max(1.4826 * mad, signal.min_scale)
    return Reading(z=z, value=current, baseline=median)


def _anscombe(count: float) -> float:
    return 2.0 * math.sqrt(count + 0.375)


@dataclass
class Candidate:
    peak_ts: float
    scores: dict[str, dict]  # signal name -> Reading.as_dict()
    triggers: list[str]
    ref: int | None = None  # set by the caller, e.g. the database id once saved

    @property
    def strength(self) -> float:
        return max((self.scores[name]["z"] for name in self.triggers), default=0.0)


@dataclass
class _Burst:
    started: int
    peak_t: int
    peak: Reading
    hot_seconds: int = 1


class MomentDetector:
    """Feed it observations; ask it for new candidates. One instance per live session."""

    def __init__(self, cfg: DetectorConfig | None = None) -> None:
        self.cfg = cfg or DetectorConfig()
        self.series: dict[str, Series] = {name: Series() for name in self.cfg.signals}
        self._evaluated: dict[str, int] = {}
        self._bursts: dict[str, _Burst] = {}
        self.moments: list[Candidate] = []  # emitted so far, newest last

    def observe(self, signal: str, t: float, value: float = 1.0) -> None:
        series = self.series.get(signal)
        if series is None:
            return
        if self.cfg.signals[signal].kind == "count":
            series.add(t, value)
        else:
            series.set(t, value)

    def begin(self, signal: str, t: float) -> None:
        """Say that a count signal is being observed from time `t`. Without this, a
        signal's history would start at its first event, and silence before it (which is
        real information: nothing was happening) would be lost."""
        series = self.series.get(signal)
        if series is not None and series.first is None:
            series.first = series.last = int(t)

    def reset(self, signal: str) -> None:
        """Forget a signal's history, e.g. after its source was disconnected: a gap in the
        data must not look like silence followed by a spike."""
        if signal in self.series:
            self.series[signal] = Series()
            self._evaluated.pop(signal, None)
            self._bursts.pop(signal, None)

    def reading(self, signal: str, t: int) -> Reading | None:
        return zscore(self.series[signal], t, self.cfg, self.cfg.signals[signal])

    def evaluate(self, now: float) -> list[tuple[str, Candidate]]:
        """Process every complete second up to `now`.

        Returns ("new", candidate) for a fresh moment, or ("update", candidate) when a
        second signal confirmed a moment that was already emitted (same object, with
        merged scores)."""
        events: list[tuple[str, Candidate]] = []
        horizon = int(now) - 1  # the current second is still filling up
        for name, signal in self.cfg.signals.items():
            series = self.series[name]
            if series.first is None:
                continue
            # A level signal (audio) arrives late and in chunks: only score what exists.
            upto = horizon if signal.kind == "count" else min(horizon, series.last or horizon)
            start = self._evaluated.get(name, series.first - 1) + 1
            for t in range(start, upto + 1):
                finished = self._step(name, signal, t)
                if finished is not None:
                    events.append(self._emit(name, finished))
            self._evaluated[name] = max(upto, self._evaluated.get(name, upto))
            series.prune(upto - self.cfg.baseline_s - 3 * self.cfg.window_s)
        return events

    def _step(self, name: str, signal: SignalConfig, t: int) -> _Burst | None:
        reading = self.reading(name, t)
        burst = self._bursts.get(name)
        hot = (
            reading is not None
            and reading.z >= signal.threshold
            and (signal.kind != "count" or reading.value >= signal.min_events)
        )
        if burst is None:
            if hot:
                self._bursts[name] = _Burst(started=t, peak_t=t, peak=reading)
            return None
        if hot:
            burst.hot_seconds += 1
        if reading is not None and reading.z > burst.peak.z:
            burst.peak_t, burst.peak = t, reading
        cooled = reading is None or reading.z < signal.threshold / 2
        if cooled or t - burst.started >= self.cfg.peak_search_s:
            del self._bursts[name]
            # A blip that crossed the line for a second or two is noise, not a moment.
            return burst if burst.hot_seconds >= self.cfg.sustain_s else None
        return None

    def _emit(self, name: str, burst: _Burst) -> tuple[str, Candidate]:
        # The window that scored highest ends at peak_t; its middle is the moment.
        peak_ts = burst.peak_t - self.cfg.window_s / 2
        scores = {name: burst.peak.as_dict()}
        for other in self.cfg.signals:
            if other != name:
                reading = self._best_nearby(other, burst.peak_t)
                if reading is not None:
                    scores[other] = reading.as_dict()
        return self.trigger(name, peak_ts, scores)

    def trigger(self, name: str, peak_ts: float, scores: dict[str, dict]) -> tuple[str, Candidate]:
        """Record a moment, or fold it into one already found within the cooldown.
        Also the entry point for signals scored outside this class (clip counts)."""
        for moment in reversed(self.moments):
            if abs(moment.peak_ts - peak_ts) <= self.cfg.cooldown_s:
                # Same moment again, or through another signal: confirm, don't duplicate.
                if name in scores and scores[name]["z"] > moment.strength:
                    moment.peak_ts = peak_ts  # the stronger peak is the real one
                if name not in moment.triggers:
                    moment.triggers.append(name)
                for key, value in scores.items():
                    if key not in moment.scores or value["z"] > moment.scores[key]["z"]:
                        moment.scores[key] = value
                return "update", moment
        candidate = Candidate(peak_ts=peak_ts, scores=scores, triggers=[name])
        self.moments.append(candidate)
        self.moments = self.moments[-50:]
        return "new", candidate

    def _best_nearby(self, name: str, t: int) -> Reading | None:
        """Strongest reading of another signal within a window either side of `t`."""
        best: Reading | None = None
        for second in range(t - self.cfg.window_s, t + self.cfg.window_s + 1):
            reading = self.reading(name, second)
            if reading is not None and (best is None or reading.z > best.z):
                best = reading
        return best
