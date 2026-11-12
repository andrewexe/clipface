"""The policy gate: every rendered clip passes through here before it can be published.

Outcome for each clip:
  block  -> rejected automatically, with the findings recorded
  flag   -> stays in the review queue for a person
  clean  -> auto-approved only if every condition for autonomy holds; a random sample
            of those still goes to a person, to measure what the gate misses
"""

from __future__ import annotations

import logging
import random
from collections.abc import Callable
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from engine.control.autonomy import AUTONOMOUS
from engine.db.audit import write_audit
from engine.db.models import Clip, Moment, Streamer, StreamSession, utcnow
from engine.policy.classifier import PolicyClassifier
from engine.policy.music import MusicReading, detect_music
from engine.policy.rules import normalise_rules
from engine.policy.text import Finding, check_text
from engine.settings import AppSettings, SettingsStore

log = logging.getLogger(__name__)

PERMITTED = ("granted", "program")


def autonomy_blockers(settings: AppSettings, streamer: Streamer, score: float | None) -> list[str]:
    """Reasons a clean clip may NOT be auto-approved. Empty means it may."""
    reasons = []
    if settings.autonomy_level not in AUTONOMOUS:
        reasons.append("autonomy is L1: every clip needs your approval")
    if settings.publishing_paused:
        reasons.append("publishing is paused")
    if streamer.permission_status not in PERMITTED:
        reasons.append("no permission is recorded for this streamer")
    if not streamer.enabled:
        reasons.append("the streamer is paused")
    if score is None:
        reasons.append("the clip was not scored")
    elif score < settings.auto_approve_score:
        needed = settings.auto_approve_score
        reasons.append(f"score {score:.2f} is below the auto-approve level of {needed:.2f}")
    return reasons


class PolicyGate:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: SettingsStore,
        classifier: PolicyClassifier | None = None,
        music: Callable[[Path], MusicReading] = detect_music,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._classifier = classifier
        self._music = music
        self._rng = rng
        self._on_approved: list[Callable[[int], None]] = []
        self._on_review: list[Callable[[int], None]] = []
        self._on_spot_check: list[Callable[[int], None]] = []
        self._spot_check = False

    def on_spot_check(self, listener: Callable[[int], None]) -> None:
        """Called with the clip id when (at L3) a clip was published and then picked for
        you to check afterwards."""
        self._on_spot_check.append(listener)

    def on_approved(self, listener: Callable[[int], None]) -> None:
        """Called with the clip id when the gate approves a clip on its own."""
        self._on_approved.append(listener)

    def on_needs_review(self, listener: Callable[[int], None]) -> None:
        """Called with the clip id when a clip is left for a person to review."""
        self._on_review.append(listener)

    def review(self, clip_id: int) -> str:
        """Check one clip and act on the result. Returns "rejected", "review" or
        "auto_approved". Never raises: a gate that crashes must not let a clip through."""
        self._spot_check = False
        try:
            action = self._review(clip_id)
        except Exception:
            log.exception("policy gate failed on clip %s", clip_id)
            action = self._record_failure(clip_id)
        listeners = list(self._on_approved if action == "auto_approved" else self._on_review)
        if action == "auto_approved" and self._spot_check:
            listeners += self._on_spot_check
        if action != "rejected":
            for listener in listeners:
                try:
                    listener(clip_id)
                except Exception:
                    log.exception("gate listener failed")
        return action

    def _review(self, clip_id: int) -> str:
        settings = self._settings.get()
        with self._session_factory() as db:
            clip = db.get(Clip, clip_id)
            if clip is None or clip.status != "pending":
                return "review"
            moment = db.get(Moment, clip.moment_id)
            streamer = db.get(Streamer, db.get(StreamSession, moment.session_id).streamer_id)
            video = Path(clip.file_path or "")
            duration = (clip.meta or {}).get("duration") or 0.0
            transcript, title, caption = clip.transcript, clip.title, clip.caption
            banned = normalise_rules(streamer.program_rules)["banned"]

        findings: list[Finding] = []
        said_and_written = " \n ".join(part for part in (transcript, title, caption) if part)
        findings += check_text(said_and_written, settings.blocked_words, banned)

        reading = self._music(video) if video.is_file() else None
        if reading is None:
            findings.append(Finding("music", "flag", "the audio could not be checked for music"))
        elif reading.likelihood >= settings.music_threshold:
            verdict = "block" if settings.music_action == "reject" else "flag"
            findings.append(Finding("music", verdict, "music was detected in the audio"))

        if self._classifier is None:
            findings.append(
                Finding("not_checked", "flag", "the content check did not run: not set up")
            )
        elif not video.is_file():
            findings.append(
                Finding("not_checked", "flag", "the content check did not run: no video file")
            )
        else:
            findings += self._classifier.check(video, duration, transcript)

        verdict = (
            "block"
            if any(f.verdict == "block" for f in findings)
            else "flag"
            if findings
            else "clean"
        )
        result: dict = {
            "verdict": verdict,
            "findings": [f.as_dict() for f in findings],
            "music": reading.as_dict() if reading else None,
            "checked_at": utcnow().isoformat(),
        }

        with self._session_factory.begin() as db:
            clip = db.get(Clip, clip_id)
            streamer = db.get(
                Streamer,
                db.get(StreamSession, db.get(Moment, clip.moment_id).session_id).streamer_id,
            )
            if verdict == "block":
                action = "rejected"
                clip.status = "rejected"
                result["decision"] = {"action": action, "why": ["a content rule was broken"]}
                audit_action = "clip.policy_reject"
            elif verdict == "flag":
                action = "review"
                result["decision"] = {"action": action, "why": ["a check needs a person"]}
                audit_action = "clip.policy_flag"
            else:
                blockers = autonomy_blockers(settings, streamer, clip.claude_score)
                if blockers:
                    action = "review"
                    result["decision"] = {"action": action, "why": blockers}
                elif self._rng() >= settings.review_sample_rate:
                    action = "auto_approved"
                    clip.status = "approved"
                    clip.meta = {**(clip.meta or {}), "auto_approved": True}
                    result["decision"] = {"action": action, "why": []}
                elif settings.autonomy_level == "L3":
                    # L3: the sample publishes like the rest and is checked afterwards.
                    action = "auto_approved"
                    self._spot_check = True
                    clip.status = "approved"
                    clip.meta = {
                        **(clip.meta or {}),
                        "auto_approved": True,
                        "spot_check": "pending",
                    }
                    result["sampled"] = True
                    result["decision"] = {
                        "action": action,
                        "why": ["published first; picked at random for you to check afterwards"],
                    }
                else:
                    action = "review"
                    result["sampled"] = True
                    result["decision"] = {
                        "action": action,
                        "why": ["picked at random to check the gate's work"],
                    }
                audit_action = (
                    "clip.auto_approve" if action == "auto_approved" else "clip.policy_pass"
                )
            clip.policy_result = result
            write_audit(
                db,
                actor="system",
                action=audit_action,
                object_id=f"clip:{clip_id}",
                details={
                    "verdict": verdict,
                    "checks": sorted({f.check for f in findings}),
                    "why": result["decision"]["why"],
                },
            )
        return action

    def _record_failure(self, clip_id: int) -> str:
        try:
            with self._session_factory.begin() as db:
                clip = db.get(Clip, clip_id)
                if clip is not None:
                    clip.policy_result = {
                        "verdict": "flag",
                        "findings": [
                            Finding(
                                "not_checked", "flag", "the policy gate failed; see the engine log"
                            ).as_dict()
                        ],
                        "checked_at": utcnow().isoformat(),
                        "decision": {"action": "review", "why": ["the gate itself failed"]},
                    }
        except Exception:
            log.exception("could not record the gate failure for clip %s", clip_id)
        return "review"
