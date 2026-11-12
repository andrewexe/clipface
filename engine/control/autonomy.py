"""Autonomy levels, and the one automatic change to them: dropping from L3 to L2.

L1  every clip needs your approval.
L2  a clean, high-scoring clip of a permitted streamer publishes on its own; a random
    sample of those is held for you to approve first.
L3  the same clips publish on their own, and nothing clean waits: the random sample is
    published too and shown to you afterwards as a spot check.

L3 changes when you look, not what may publish. Every content rule, the permission
requirement, the score level, the caps and the kill switch are the same at L2 and L3.
L3 unlocks after two weeks of L2 with no strikes, and is lost automatically on a strike
or a failed spot check; the two weeks then start again.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session, sessionmaker

from engine.db.audit import write_audit
from engine.db.models import utcnow
from engine.settings import SettingsStore

log = logging.getLogger(__name__)

AUTONOMOUS = ("L2", "L3")


def demote_to_l2(
    settings: SettingsStore, session_factory: sessionmaker[Session], reason: str
) -> bool:
    """If autonomy is L3, drop it to L2 and restart the two-week clock. Returns whether
    it changed anything."""
    if settings.get().autonomy_level != "L3":
        return False
    settings.update({"autonomy_level": "L2", "autonomy_l2_since": utcnow().isoformat()})
    with session_factory.begin() as db:
        write_audit(db, actor="system", action="autonomy.demote", details={"reason": reason})
    log.warning("autonomy dropped from L3 to L2: %s", reason)
    return True
