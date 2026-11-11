"""Rolling buffer bookkeeping, using empty files with chosen timestamps."""

from __future__ import annotations

import os

import pytest

from engine.recorder.store import SegmentStore

T0 = 1_800_000_000.0
MB = 1024 * 1024


def make(store, session_id, index, end, size=MB, run="100", listed=True, closed_late=0.0):
    """One 10-second segment whose content ends at `end` (clock time)."""
    folder = store.session_dir(session_id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{run}_{index:06d}.ts"
    with path.open("wb") as handle:
        handle.truncate(size)
    os.utime(path, (end + closed_late, end + closed_late))
    if listed:
        with (folder / f"{run}.csv").open("a") as handle:
            handle.write(f"{path.name},{index * 10:.6f},{(index + 1) * 10:.6f}\n")
    return path


@pytest.fixture
def store(tmp_path):
    return SegmentStore(tmp_path / "buffer", segment_seconds=10)


def test_timeline_is_contiguous_from_mtimes(store):
    for i in range(4):
        make(store, 1, i, T0 + 10 * (i + 1))
    segs = store.segments(1)
    assert [(s.start - T0, s.end - T0) for s in segs] == [(0, 10), (10, 20), (20, 30), (30, 40)]
    assert all(s.duration == 10 for s in segs)


def test_file_being_written_is_excluded(store):
    for i in range(3):
        make(store, 1, i, T0 + 10 * (i + 1), listed=i < 2)
    assert len(store.segments(1)) == 2


def test_burst_arrival_does_not_distort_the_timeline(store):
    # The first three segments were all closed at T0+30 (startup burst), later ones on time.
    for i in range(3):
        make(store, 1, i, T0 + 30)
    for i in range(3, 8):
        make(store, 1, i, T0 + 10 * (i + 1))
    segs = store.segments(1)
    assert [round(s.start - T0) for s in segs] == [0, 10, 20, 30, 40, 50, 60, 70]


def test_late_closes_are_smoothed_out(store):
    for i in range(12):
        make(store, 1, i, T0 + 10 * (i + 1), closed_late=4.0 if i % 3 else 0.0)
    segs = store.segments(1)
    assert [round(s.end - T0) for s in segs] == [10 * (i + 1) for i in range(12)]


def test_half_written_list_lines_are_ignored(store):
    make(store, 1, 0, T0 + 10)
    with (store.session_dir(1) / "100.csv").open("a") as handle:
        handle.write("100_000001.ts,10.0")  # ffmpeg is mid-write
    assert len(store.segments(1)) == 1


def test_gap_or_new_run_does_not_stretch_a_segment(store):
    make(store, 1, 0, T0 + 10)
    make(store, 1, 1, T0 + 20)
    make(store, 1, 0, T0 + 500, run="200")  # recorder restarted after a dropout
    make(store, 1, 1, T0 + 510, run="200")
    segs = store.segments(1)
    assert [round(s.duration) for s in segs] == [10, 10, 10, 10]
    assert [round(s.start - T0) for s in segs] == [0, 10, 490, 500]


def test_window_returns_overlapping_segments(store):
    for i in range(30):
        make(store, 1, i, T0 + 10 * (i + 1))
    hit = store.window(1, T0 + 95, T0 + 135)
    assert [(s.start - T0, s.end - T0) for s in hit] == [
        (90, 100),
        (100, 110),
        (110, 120),
        (120, 130),
        (130, 140),
    ]
    assert store.window(1, T0 + 1000, T0 + 1100) == []


def test_extract_survives_pruning(store, tmp_path):
    for i in range(6):
        make(store, 1, i, T0 + 10 * (i + 1))
    copies = store.extract(1, T0 + 15, T0 + 35, tmp_path / "raw" / "moment_1")
    assert [c.path.name for c in copies] == ["100_000001.ts", "100_000002.ts", "100_000003.ts"]
    store.prune(
        T0 + 10_000, active=set(), keep_seconds=60, ended_retention_seconds=60, cap_bytes=10**12
    )
    assert store.session_ids() == []
    assert all(c.path.exists() and c.path.stat().st_size == MB for c in copies)
    assert copies[0].start == T0 + 10  # timing is carried over


def test_rolling_window_keeps_last_n_seconds(store):
    for i in range(200):  # 2000 s of footage
        make(store, 1, i, T0 + 10 * (i + 1))
    now = T0 + 2000
    result = store.prune(
        now, active={1}, keep_seconds=1200, ended_retention_seconds=21600, cap_bytes=10**12
    )
    segs = store.segments(1)
    assert result.rolled == 79
    assert min(s.end for s in segs) >= now - 1200
    assert len(segs) == 121


def test_never_deletes_the_file_being_written(store):
    make(store, 1, 0, T0, listed=False)  # ancient-looking, but the newest file of a live run
    result = store.prune(
        T0 + 99_999, active={1}, keep_seconds=60, ended_retention_seconds=60, cap_bytes=0
    )
    assert result.rolled == 0 and result.capped == 0
    assert len(list(store.session_dir(1).glob("*.ts"))) == 1


def test_ended_sessions_expire_after_retention(store):
    make(store, 1, 0, T0)
    make(store, 2, 0, T0 + 20_000)
    result = store.prune(
        T0 + 22_000,
        active=set(),
        keep_seconds=10**9,
        ended_retention_seconds=21_600,
        cap_bytes=10**12,
    )
    assert result.expired_sessions == 1
    assert store.session_ids() == [2]


def test_disk_cap_drops_oldest_first_across_streamers(store):
    for i in range(10):
        make(store, 1, i, T0 + 10 * (i + 1))  # older
        make(store, 2, i, T0 + 1000 + 10 * (i + 1))  # newer
    result = store.prune(
        T0 + 1200,
        active={1, 2},
        keep_seconds=10**9,
        ended_retention_seconds=10**9,
        cap_bytes=12 * MB,
    )
    assert result.total_bytes <= 12 * MB
    assert result.capped == 8
    assert len(store.segments(1)) == 2 and len(store.segments(2)) == 10


def test_one_hour_stays_under_cap_and_5_minutes_ago_is_retrievable(store):
    """Phase 2 check, simulated: three streamers for an hour, pruning every 10 seconds."""
    cap = 600 * MB
    keep = 1200
    peak = 0
    for step in range(360):  # one hour of 10-second segments
        now = T0 + 10 * (step + 1)
        for session_id, size in ((1, 3 * MB), (2, 2 * MB), (3, 1 * MB)):
            make(store, session_id, step, now, size=size)
        result = store.prune(
            now, active={1, 2, 3}, keep_seconds=keep, ended_retention_seconds=21_600, cap_bytes=cap
        )
        peak = max(peak, store.total_bytes())
        assert result.total_bytes == store.total_bytes()
    assert peak <= cap + 6 * MB  # at most the three files being written above the cap
    now = T0 + 3600
    for session_id in (1, 2, 3):
        five_minutes_ago = store.window(session_id, now - 310, now - 290)
        assert five_minutes_ago, f"session {session_id} lost footage from 5 minutes ago"
        assert all(seg.path.exists() for seg in five_minutes_ago)
    # Uncapped, 20 minutes of all three would be 720 MB: the cap is what held it at 600.
    assert store.total_bytes() > 500 * MB


def test_empty_and_missing_folders(store):
    assert store.segments(42) == [] and store.session_ids() == [] and store.total_bytes() == 0
    result = store.prune(T0, active={1}, keep_seconds=1, ended_retention_seconds=1, cap_bytes=0)
    assert result.total_bytes == 0
