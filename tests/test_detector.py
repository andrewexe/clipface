"""Highlight detector. Includes the Phase 3 check: on chat logs with known highlights it
finds them, and the false-positive rate is set by the threshold."""

from __future__ import annotations

from dataclasses import replace

import pytest

from engine.detector.core import DetectorConfig, MomentDetector, Series, SignalConfig, zscore
from engine.detector.replay import load_chat_log, replay, score, with_threshold
from tests.chatlogs import synthetic_chat

CHAT_ONLY = DetectorConfig(signals={"chat": SignalConfig(threshold=3.0, min_events=8)})
CHAT_HYPE = replace(
    DetectorConfig(),
    signals={
        "chat": SignalConfig(threshold=3.0, min_events=8),
        "hype": SignalConfig(threshold=3.0, min_events=6),
    },
)


def steady(detector, signal, start, seconds, per_second):
    for second in range(start, start + seconds):
        for i in range(per_second):
            detector.observe(signal, second + i / per_second)


# -- z-score ------------------------------------------------------------------------


def test_no_reading_without_enough_history():
    detector = MomentDetector(CHAT_ONLY)
    steady(detector, "chat", 0, 60, 3)
    assert detector.reading("chat", 59) is None
    steady(detector, "chat", 60, 60, 3)
    assert detector.reading("chat", 119) is not None


def test_steady_chat_scores_near_zero_and_a_spike_scores_high():
    detector = MomentDetector(CHAT_ONLY)
    steady(detector, "chat", 0, 300, 3)
    calm = detector.reading("chat", 299)
    assert abs(calm.z) < 1 and calm.baseline == 30
    steady(detector, "chat", 300, 10, 15)
    spike = detector.reading("chat", 309)
    assert spike.z > 10 and spike.value == 150


def test_quiet_chat_cannot_trigger_on_a_handful_of_messages():
    detector = MomentDetector(CHAT_ONLY)
    for second in range(0, 400, 20):  # one message every 20 seconds
        detector.observe("chat", second)
    for i in range(5):  # then five at once: relatively huge, absolutely nothing
        detector.observe("chat", 400 + i * 0.5)
    assert detector.evaluate(460) == []
    assert detector.reading("chat", 403).z >= 3  # it does look like a spike...
    assert detector.moments == []  # ...but min_events holds it back


def test_level_signals_use_the_average_and_ignore_gaps():
    series = Series()
    for second in range(0, 300):
        series.set(second, -30.0 + (second % 3) * 0.5)
    cfg = DetectorConfig(signals={"audio": SignalConfig(4.0, kind="level", min_scale=1.5)})
    for second in range(300, 310):
        series.set(second, -12.0)
    reading = zscore(series, 309, cfg, cfg.signals["audio"])
    assert reading.z > 8 and reading.value == -12.0
    assert zscore(series, 400, cfg, cfg.signals["audio"]) is None  # no data there


# -- bursts, peaks, cooldown ----------------------------------------------------------


def test_one_spike_makes_one_moment_at_the_peak():
    detector = MomentDetector(CHAT_ONLY)
    steady(detector, "chat", 0, 300, 3)
    steady(detector, "chat", 300, 4, 6)  # ramp
    steady(detector, "chat", 304, 6, 25)  # peak
    steady(detector, "chat", 310, 120, 3)
    events = detector.evaluate(431)
    assert [kind for kind, _ in events] == ["new"]
    moment = events[0][1]
    assert 298 <= moment.peak_ts <= 312
    assert moment.triggers == ["chat"] and moment.scores["chat"]["z"] > 3


def test_cooldown_merges_spikes_within_60_seconds_and_separates_later_ones():
    detector = MomentDetector(CHAT_ONLY)
    steady(detector, "chat", 0, 300, 3)
    steady(detector, "chat", 300, 8, 25)
    steady(detector, "chat", 308, 22, 3)
    steady(detector, "chat", 330, 8, 25)  # 30 s later: same moment
    steady(detector, "chat", 338, 112, 3)
    steady(detector, "chat", 450, 8, 25)  # 150 s later: a new one
    steady(detector, "chat", 458, 60, 3)
    events = detector.evaluate(520)
    assert [kind for kind, _ in events] == ["new", "update", "new"]
    assert len(detector.moments) == 2
    assert detector.moments[1].peak_ts - detector.moments[0].peak_ts > 100


def test_a_highlight_does_not_blind_the_detector_to_the_next_one():
    detector = MomentDetector(CHAT_ONLY)
    steady(detector, "chat", 0, 300, 3)
    steady(detector, "chat", 300, 12, 30)
    steady(detector, "chat", 312, 90, 3)
    steady(detector, "chat", 402, 10, 14)  # weaker, 100 s later, baseline still holds the first
    steady(detector, "chat", 412, 60, 3)
    detector.evaluate(470)
    assert len(detector.moments) == 2


def test_second_signal_confirms_instead_of_duplicating():
    detector = MomentDetector(CHAT_HYPE)
    steady(detector, "chat", 0, 300, 4)
    for second in range(0, 300, 2):
        detector.observe("hype", second)
    steady(detector, "chat", 300, 10, 20)
    steady(detector, "hype", 302, 10, 12)
    steady(detector, "chat", 310, 60, 4)
    events = detector.evaluate(372)
    assert len(detector.moments) == 1
    moment = detector.moments[0]
    assert set(moment.triggers) == {"chat", "hype"}
    assert {"chat", "hype"} <= set(moment.scores)
    assert sorted(kind for kind, _ in events) == ["new", "update"]


def test_late_arriving_audio_is_scored_when_it_arrives():
    cfg = DetectorConfig(signals={"audio": SignalConfig(4.0, kind="level", min_scale=1.5)})
    detector = MomentDetector(cfg)
    for second in range(0, 300):
        detector.observe("audio", second, -32.0)
    assert detector.evaluate(330) == []  # nothing new; clock moved on, data did not
    for second in range(300, 310):
        detector.observe("audio", second, -10.0)
    for second in range(310, 340):
        detector.observe("audio", second, -32.0)
    events = detector.evaluate(345)
    assert [kind for kind, _ in events] == ["new"]
    assert 298 <= events[0][1].peak_ts <= 310


def test_reset_forgets_history_so_a_reconnect_is_not_a_spike():
    detector = MomentDetector(CHAT_ONLY)
    steady(detector, "chat", 0, 300, 1)
    detector.evaluate(300)
    detector.reset("chat")
    steady(detector, "chat", 900, 60, 20)  # chat came back after a 10-minute outage
    assert detector.evaluate(961) == []


def test_external_trigger_merges_with_cooldown():
    detector = MomentDetector(CHAT_ONLY)
    kind, first = detector.trigger("clips", 1000.0, {"clips": {"z": 4.0, "value": 4}})
    again, same = detector.trigger("clips", 1030.0, {"clips": {"z": 6.0, "value": 6}})
    later, other = detector.trigger("clips", 1200.0, {"clips": {"z": 3.0, "value": 3}})
    assert (kind, again, later) == ("new", "update", "new")
    assert same is first and first.scores["clips"]["value"] == 6 and other is not first


def test_memory_stays_bounded():
    detector = MomentDetector(CHAT_ONLY)
    for second in range(0, 7200):
        detector.observe("chat", second)
        detector.observe("chat", second + 0.5)
        if second % 10 == 0:
            detector.evaluate(second)
    assert len(detector.series["chat"].values) < 400


# -- the Phase 3 check: known highlights ----------------------------------------------


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_finds_known_highlights_in_an_hour_of_chat(seed):
    events, truth = synthetic_chat(seed)
    found = replay(events, CHAT_HYPE)
    result = score(found, truth, duration_s=3600)
    assert result["recall"] == 1.0, result
    assert result["false_positives_per_hour"] <= 6, result


def test_false_positive_rate_is_set_by_the_threshold():
    totals = {}
    for z in (2.0, 3.0, 4.0, 6.0):
        cfg = with_threshold(CHAT_HYPE, z)
        false_positives = found = highlights = 0
        for seed in range(1, 9):
            events, truth = synthetic_chat(seed, flurries=20)
            result = score(replay(events, cfg), truth, duration_s=3600)
            false_positives += result["false_positives"]
            found += result["found"]
            highlights += result["highlights"]
        totals[z] = (false_positives / 8, found / highlights)
    per_hour = [totals[z][0] for z in (2.0, 3.0, 4.0, 6.0)]
    assert per_hour == sorted(per_hour, reverse=True), totals  # stricter -> fewer
    assert per_hour[0] > per_hour[-1]
    assert totals[6.0][0] <= 0.5, totals  # strict setting: almost none
    assert totals[3.0][1] >= 0.95, totals  # default still finds the highlights
    assert totals[3.0][0] <= 6, totals


def test_weak_highlights_are_the_price_of_a_strict_threshold():
    recalls = {}
    for z in (3.0, 9.0):
        hits = total = 0
        for seed in range(1, 6):
            events, truth = synthetic_chat(seed, strength=2.2)
            result = score(replay(events, with_threshold(CHAT_HYPE, z)), truth, 3600)
            hits += result["found"]
            total += result["highlights"]
        recalls[z] = hits / total
    assert recalls[3.0] > recalls[9.0], recalls
    assert recalls[3.0] >= 0.9, recalls


def test_no_highlights_means_few_moments():
    events, _ = synthetic_chat(seed=11, highlights=0, flurries=0)
    assert len(replay(events, CHAT_HYPE)) <= 3


def test_replay_reads_recorded_log_format(tmp_path):
    path = tmp_path / "chat.jsonl"
    lines = [
        '{"t": 1.5, "ch": "a", "n": 4, "hype": 1}',
        '{"t": 2.0, "ch": "b", "n": 9, "hype": 0}',
        "garbage",
        '{"t": 0.5, "ch": "a", "n": 2, "hype": 0}',
    ]
    path.write_text("\n".join(lines))
    events = load_chat_log(path, channel="a")
    assert events == [(0.5, "chat", 1.0), (1.5, "chat", 1.0), (1.5, "hype", 1.0)]
    assert len(load_chat_log(path)) == 4
