"""Run the detector over a recorded log, to tune thresholds and measure false positives.

    python -m engine.detector.replay chat.jsonl --channel forsen --z 3.0
    python -m engine.detector.replay chat.jsonl --truth 312,958,1440

Log format: one JSON object per line with "t" (seconds), optional "ch" (channel) and
"hype" (0/1). `tests/record_chat.py` writes this format from live Twitch chat.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path

from engine.detector.core import Candidate, DetectorConfig, MomentDetector, SignalConfig


def replay(
    events: Iterable[tuple[float, str, float]], cfg: DetectorConfig | None = None
) -> list[Candidate]:
    """events: (time, signal, value), in time order. Returns the moments found."""
    detector = MomentDetector(cfg)
    last = None
    for t, signal, value in events:
        if last is None:
            for name, config in detector.cfg.signals.items():
                if config.kind == "count":
                    detector.begin(name, t)
        if last is not None and int(t) > int(last):
            detector.evaluate(t)
        detector.observe(signal, t, value)
        last = t
    if last is not None:
        horizon = last + detector.cfg.peak_search_s + detector.cfg.window_s + 2
        detector.evaluate(horizon)
    return list(detector.moments)


def with_threshold(cfg: DetectorConfig, z: float) -> DetectorConfig:
    """The same config with every count signal's trigger set to `z`."""
    signals = {
        name: replace(signal, threshold=z) if signal.kind == "count" else signal
        for name, signal in cfg.signals.items()
    }
    return replace(cfg, signals=signals)


def score(
    found: list[Candidate], truth: list[float], duration_s: float, tolerance_s: float = 25.0
) -> dict:
    """Match found moments to known highlight times."""
    matched: set[int] = set()
    false_positives = 0
    for candidate in found:
        hits = [i for i, t in enumerate(truth) if abs(candidate.peak_ts - t) <= tolerance_s]
        if hits:
            matched.update(hits)
        else:
            false_positives += 1
    hours = max(duration_s / 3600, 1e-9)
    return {
        "highlights": len(truth),
        "found": len(matched),
        "missed": len(truth) - len(matched),
        "recall": len(matched) / len(truth) if truth else None,
        "false_positives": false_positives,
        "false_positives_per_hour": false_positives / hours,
    }


def load_chat_log(path: Path, channel: str | None = None) -> list[tuple[float, str, float]]:
    events: list[tuple[float, str, float]] = []
    for line in path.read_text().splitlines():
        try:
            row = json.loads(line)
            t = float(row["t"])
        except (ValueError, KeyError, TypeError):
            continue
        if channel and row.get("ch") != channel:
            continue
        events.append((t, "chat", 1.0))
        if row.get("hype"):
            events.append((t, "hype", 1.0))
    events.sort(key=lambda event: event[0])
    return events


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("log", type=Path)
    parser.add_argument("--channel")
    parser.add_argument("--z", type=float, default=3.0)
    parser.add_argument("--truth", help="comma-separated highlight times in seconds")
    args = parser.parse_args()

    events = load_chat_log(args.log, args.channel)
    if not events:
        raise SystemExit("no events in that log")
    cfg = with_threshold(DetectorConfig(), args.z)
    cfg = replace(cfg, signals={k: v for k, v in cfg.signals.items() if k != "audio"})
    found = replay(events, cfg)
    duration = events[-1][0] - events[0][0]
    print(f"{len(events)} events over {duration / 60:.1f} min, z >= {args.z}")
    for moment in found:
        parts = ", ".join(
            f"{k} z={v['z']} ({v['value']:.0f} vs {v['baseline']:.0f})"
            for k, v in moment.scores.items()
        )
        print(f"  {moment.peak_ts:8.0f}s  triggers={'+'.join(moment.triggers)}  {parts}")
    print(f"{len(found)} moments = {len(found) / max(duration / 3600, 1e-9):.1f} per hour")
    if args.truth:
        truth = [float(x) for x in args.truth.split(",")]
        print(json.dumps(score(found, truth, duration), indent=2))


if __name__ == "__main__":
    main()


__all__ = ["SignalConfig", "load_chat_log", "replay", "score", "with_threshold"]
