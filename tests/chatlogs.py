"""Synthetic chat logs with known highlights, for detector tests.

The background is deliberately awkward: the message rate drifts, it is burstier than a
Poisson process, and there are small non-highlight flurries (someone asks a question,
a sub train) that a good detector should mostly ignore.
"""

from __future__ import annotations

import math
import random


def poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    if lam > 30:
        return max(0, round(rng.gauss(lam, math.sqrt(lam))))
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def synthetic_chat(
    seed: int,
    duration_s: int = 3600,
    base_rate: float = 4.0,
    highlights: int = 6,
    strength: float = 5.0,
    flurries: int = 12,
) -> tuple[list[tuple[float, str, float]], list[float]]:
    """Returns (events, highlight times). Events are (t, signal, 1.0) in time order."""
    rng = random.Random(seed)
    slots = list(range(400, duration_s - 120, 240))
    truth = sorted(rng.sample(slots, highlights))
    truth = [t + rng.uniform(-40, 40) for t in truth]
    flurry_times = [rng.uniform(200, duration_s - 60) for _ in range(flurries)]

    events: list[tuple[float, str, float]] = []
    wander = 0.0
    for second in range(duration_s):
        wander = 0.995 * wander + rng.gauss(0, 0.02)
        rate = base_rate * (1 + 0.35 * math.sin(second / 500) + wander)
        rate *= rng.gammavariate(4, 0.25)  # burstier than Poisson
        hype_share = 0.10
        for t in truth:
            if 0 <= second - t < 14:  # reaction: fast rise, slower decay
                rate *= 1 + (strength - 1) * math.exp(-(second - t) / 6)
                hype_share = 0.65
        for t in flurry_times:
            if 0 <= second - t < 6:
                rate *= 1.7
        for _ in range(poisson(rng, max(rate, 0.0))):
            stamp = second + rng.random()
            events.append((stamp, "chat", 1.0))
            if rng.random() < hype_share:
                events.append((stamp, "hype", 1.0))
    events.sort(key=lambda event: event[0])
    return events, truth
