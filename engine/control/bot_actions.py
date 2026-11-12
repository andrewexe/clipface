"""What the phone bot is allowed to do, in terms of the engine. Nothing else is exposed."""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import func, select

from engine.control.telegram import Card
from engine.db.audit import write_audit
from engine.db.models import Clip, PublishJob


def set_publishing_paused(ctx, paused: bool, via: str | None = None, actor: str = "user") -> bool:
    """The kill switch. Returns whether anything changed."""
    _settings, changes = ctx.settings.update({"publishing_paused": paused})
    if changes:
        with ctx.session_factory.begin() as db:
            write_audit(
                db,
                actor=actor,
                action="publishing.pause" if paused else "publishing.resume",
                details={"via": via} if via else None,
            )
        for service in ctx.services():
            if service is not None:
                service.wake()
    return bool(changes)


class EngineBotActions:
    def __init__(self, ctx) -> None:
        self._ctx = ctx

    def card(self, clip_id: int) -> Card | None:
        from engine.api.clips import _row, _view

        with self._ctx.session_factory() as db:
            try:
                view = _view(*_row(db, clip_id))
                path = db.get(Clip, clip_id).file_path
            except HTTPException:
                return None
        if view["status"] != "pending":
            return None
        lines = [
            view["title"] or "Untitled clip",
            f"{view['streamer']['name']} · {round(view['duration'])} s"
            + (f" · score {view['score']:.2f}" if view["score"] is not None else ""),
        ]
        policy = view["policy"] or {}
        findings = [f["detail"] for f in policy.get("findings", [])][:5]
        if findings:
            lines.append("⚠️ " + "; ".join(findings))
        elif policy.get("verdict") == "clean":
            lines.append("Safety checks passed.")
        if view["streamer"]["permission_status"] not in ("granted", "program"):
            lines.append("No permission recorded: approving exports the file only.")
        return Card(caption="\n".join(lines), video=Path(path) if path else None)

    def _review(self, clip_id: int, new: str) -> str:
        from engine.api.clips import _set_status

        try:
            view = _set_status(self._ctx, clip_id, new, ("pending",), via="telegram")
        except HTTPException as exc:
            if exc.status_code == 404:
                return "That clip no longer exists."
            return "That clip was already handled."
        if new == "rejected":
            return "❌ Rejected."
        posts = view.get("scheduled_posts") or 0
        if posts:
            return f"✅ Approved. {posts} post(s) scheduled."
        if view.get("export_error"):
            return "✅ Approved, but it could not be exported."
        return "✅ Approved and exported. Nothing was posted."

    def approve(self, clip_id: int) -> str:
        return self._review(clip_id, "approved")

    def reject(self, clip_id: int) -> str:
        return self._review(clip_id, "rejected")

    def set_paused(self, paused: bool) -> str:
        set_publishing_paused(self._ctx, paused, via="telegram")
        return "⏸ All publishing is paused." if paused else "▶️ Publishing is back on."

    def status(self) -> str:
        settings = self._ctx.settings.get()
        with self._ctx.session_factory() as db:
            pending = db.scalar(select(func.count()).where(Clip.status == "pending"))
            scheduled = db.scalar(select(func.count()).where(PublishJob.status == "scheduled"))
        return (
            f"Publishing: {'PAUSED' if settings.publishing_paused else 'on'}\n"
            f"Autonomy: {settings.autonomy_level}\n"
            f"Clips waiting for review: {pending}\n"
            f"Posts scheduled: {scheduled}"
        )

    def paired(self, chat_id: int) -> None:
        self._ctx.settings.update({"telegram_chat_id": chat_id})
        with self._ctx.session_factory.begin() as db:
            write_audit(db, actor="user", action="telegram.pair")
