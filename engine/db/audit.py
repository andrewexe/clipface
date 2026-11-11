"""Every state change writes a row to audit_log. Details must never hold secret values."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from engine.db.models import AUDIT_ACTORS, AuditLog


def write_audit(
    session: Session,
    *,
    actor: str,
    action: str,
    object_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> AuditLog:
    """Adds the entry to the session; the caller commits it with the change it records."""
    if actor not in AUDIT_ACTORS:
        raise ValueError(f"unknown actor: {actor}")
    entry = AuditLog(actor=actor, action=action, object_id=object_id, details=details)
    session.add(entry)
    return entry
