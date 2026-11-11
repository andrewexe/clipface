"""Phase 2 soak test: record a generated live stream for an hour with the shipped
settings (10-second segments, 20-minute buffer) and a deliberately small disk cap.

    uv run python -m tests.soak_recorder --minutes 60 --cap-mb 100 --out result.json

Passes if disk use never exceeds the cap (plus the one file being written) and footage
from five minutes ago can still be cut out at the end.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from pathlib import Path

from engine.db.models import Streamer, StreamSession, utcnow
from engine.db.session import create_db_engine, make_session_factory, run_migrations
from engine.recorder.service import Recorder
from engine.recorder.store import SegmentStore
from engine.settings import SettingsStore
from tests.media import TestPatternSource, probe


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--minutes", type=float, default=60)
    parser.add_argument("--cap-mb", type=float, default=100)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    work = Path(tempfile.mkdtemp(prefix="clipper-soak-"))
    db_engine = create_db_engine(work / "soak.db")
    run_migrations(db_engine)
    factory = make_session_factory(db_engine)
    with factory.begin() as db:
        db.add(Streamer(platform="twitch", username="soak"))
        db.flush()
        db.add(StreamSession(streamer_id=1, started_at=utcnow()))
    settings = SettingsStore(work / "settings.json")
    cap_bytes = int(args.cap_mb * 1024 * 1024)
    settings._current = settings._current.model_copy(update={"disk_cap_gb": cap_bytes / 1024**3})
    store = SegmentStore(work / "buffer")
    recorder = Recorder(
        factory, store, settings, source=TestPatternSource(size="640x360", bitrate="1000k")
    )

    started = time.time()
    peak = 0
    samples = []
    try:
        while time.time() - started < args.minutes * 60:
            recorder.reconcile()
            on_disk = store.total_bytes()
            peak = max(peak, on_disk)
            segs = store.segments(1)
            if int(time.time() - started) % 300 < 5:
                samples.append(
                    {
                        "minute": round((time.time() - started) / 60, 1),
                        "mb_on_disk": round(on_disk / 1024**2, 1),
                        "segments": len(segs),
                        "oldest_age_min": round((time.time() - segs[0].start) / 60, 1)
                        if segs
                        else None,
                        "recording": recorder.is_recording(1),
                    }
                )
            time.sleep(5)
        now = time.time()
        copies = store.extract(1, now - 310, now - 290, work / "raw" / "five_minutes_ago")
        playable = [probe(c.path) for c in copies]
        segs = store.segments(1)
        largest = max((seg.size for seg in segs), default=0)
        result = {
            "minutes": args.minutes,
            "cap_mb": args.cap_mb,
            "peak_mb": round(peak / 1024**2, 1),
            "stayed_under_cap": peak <= cap_bytes + 2 * largest,
            "five_minutes_ago_segments": len(copies),
            "five_minutes_ago_playable": bool(copies) and all(p["video"] for p in playable),
            "five_minutes_ago_seconds": round(sum(p["duration"] or 0 for p in playable), 1),
            "segments_at_end": len(segs),
            "buffered_minutes_at_end": round(sum(s.duration for s in segs) / 60, 1),
            "restarts": len(recorder._source.opened) - 1,
            "samples": samples,
        }
        result["passed"] = result["stayed_under_cap"] and result["five_minutes_ago_playable"]
    finally:
        recorder.stop_all()
        db_engine.dispose()
    args.out.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
