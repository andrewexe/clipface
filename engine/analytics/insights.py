"""Feedback from published clips: do higher-scored and stronger-triggered clips really
do better, and should a threshold move?

This only reports and suggests. Changing a threshold is always your decision: nothing
here edits a setting.
"""

from __future__ import annotations

import statistics
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from engine.db.models import Clip, Metric, Moment, PublishJob, utcnow
from engine.settings import AppSettings

MIN_POSTS = 20  # fewer than this and any pattern is noise
MIN_AGE = timedelta(hours=24)  # views need a day to mean something
MIN_GROUP = 5
SCORE_BUCKETS = ((0.0, 0.6, "below 0.6"), (0.6, 0.8, "0.6 to 0.8"), (0.8, 1.01, "0.8 and up"))


def _median(values: list[int]) -> float:
    return float(statistics.median(values)) if values else 0.0


def collect(db: Session) -> list[dict]:
    """One row per post that has been up for a day: its latest views, score, triggers."""
    cutoff = utcnow() - MIN_AGE
    rows = db.execute(
        select(PublishJob.id, PublishJob.scheduled_at, Clip.claude_score, Moment.signal_scores)
        .join(Clip, Clip.id == PublishJob.clip_id)
        .join(Moment, Moment.id == Clip.moment_id)
        .where(PublishJob.status == "succeeded", PublishJob.scheduled_at <= cutoff)
    ).all()
    posts = []
    for job_id, _posted, score, signals in rows:
        latest = db.scalars(
            select(Metric)
            .where(Metric.job_id == job_id)
            .order_by(Metric.checked_at.desc())
            .limit(1)
        ).first()
        if latest is None:
            continue
        signals = signals or {}
        triggers = signals.get("triggers") or []
        strength = max(
            (
                signals[t]["z"]
                for t in triggers
                if isinstance(signals.get(t), dict) and "z" in signals[t]
            ),
            default=None,
        )
        posts.append(
            {"views": latest.views, "score": score, "triggers": triggers, "strength": strength}
        )
    return posts


def analyse(posts: list[dict], settings: AppSettings) -> dict:
    views = [p["views"] for p in posts]
    overall = _median(views)
    by_score = []
    for low, high, label in SCORE_BUCKETS:
        group = [p["views"] for p in posts if p["score"] is not None and low <= p["score"] < high]
        by_score.append({"label": label, "posts": len(group), "median_views": _median(group)})
    unscored = [p["views"] for p in posts if p["score"] is None]
    if unscored:
        by_score.append(
            {"label": "not scored", "posts": len(unscored), "median_views": _median(unscored)}
        )
    by_trigger = []
    for trigger in ("chat", "hype", "audio", "clips"):
        group = [p["views"] for p in posts if trigger in p["triggers"]]
        if group:
            by_trigger.append(
                {"label": trigger, "posts": len(group), "median_views": _median(group)}
            )

    suggestions: list[dict] = []
    enough = len(posts) >= MIN_POSTS
    if enough and overall > 0:
        # Auto-approve level: the lowest level at which clips do at least as well as usual.
        for level in (0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9):
            group = [p["views"] for p in posts if p["score"] is not None and p["score"] >= level]
            if len(group) >= MIN_GROUP and _median(group) >= overall:
                if abs(level - settings.auto_approve_score) >= 0.05:
                    direction = "lower" if level < settings.auto_approve_score else "higher"
                    suggestions.append(
                        {
                            "setting": "auto_approve_score",
                            "current": settings.auto_approve_score,
                            "suggested": level,
                            "why": f"Clips scored {level:.2f} and up have a median of "
                            f"{_median(group):.0f} views against {overall:.0f} overall, so a "
                            f"{direction} auto-approve level fits what performs.",
                        }
                    )
                break
        # Detection: do the weakest triggers pull their weight?
        weak = [p["views"] for p in posts if p["strength"] is not None and p["strength"] < 4]
        strong = [p["views"] for p in posts if p["strength"] is not None and p["strength"] >= 4]
        if (
            len(weak) >= MIN_GROUP
            and len(strong) >= MIN_GROUP
            and _median(weak) < 0.5 * _median(strong)
            and settings.detect_z_threshold < 4
        ):
            suggestions.append(
                {
                    "setting": "detect_z_threshold",
                    "current": settings.detect_z_threshold,
                    "suggested": 4.0,
                    "why": f"Moments that barely passed detection have a median of "
                    f"{_median(weak):.0f} views against {_median(strong):.0f} for stronger "
                    "ones, so a stricter threshold would cut weak clips.",
                }
            )
    return {
        "posts": len(posts),
        "enough_data": enough,
        "needed": MIN_POSTS,
        "median_views": overall,
        "by_score": by_score,
        "by_trigger": by_trigger,
        "suggestions": suggestions,
    }
