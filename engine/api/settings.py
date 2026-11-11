"""Settings routes. Secret values go in; they never come back out."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, SecretStr

from engine.api.app import AppContext, get_ctx
from engine.db.audit import write_audit
from engine.keychain import KNOWN_SECRETS, MAX_SECRET_LEN, KeychainUnavailable

log = logging.getLogger(__name__)
router = APIRouter(prefix="/settings")


class SecretIn(BaseModel):
    value: SecretStr = Field(min_length=1, max_length=MAX_SECRET_LEN)


def _spec_or_404(name: str):
    spec = KNOWN_SECRETS.get(name)
    if spec is None:
        raise HTTPException(status_code=404, detail="unknown setting")
    return spec


def _unavailable(exc: KeychainUnavailable) -> HTTPException:
    return HTTPException(status_code=503, detail=str(exc))


@router.get("/secrets")
def list_secrets(ctx: AppContext = Depends(get_ctx)) -> dict:
    backend = ctx.secrets.backend()
    items = []
    for spec in KNOWN_SECRETS.values():
        try:
            is_set = ctx.secrets.is_set(spec.name)
        except KeychainUnavailable:
            is_set = False
        items.append(
            {
                "name": spec.name,
                "label": spec.label,
                "group": spec.group,
                "help": spec.help,
                "is_set": is_set,
            }
        )
    return {"keychain": backend, "secrets": items}


@router.put("/secrets/{name}")
def set_secret(name: str, body: SecretIn, ctx: AppContext = Depends(get_ctx)) -> dict:
    _spec_or_404(name)
    # Pasted keys often carry stray whitespace or a trailing newline.
    value = body.value.get_secret_value().strip()
    if not value or any(ch.isspace() or not ch.isprintable() for ch in value):
        raise HTTPException(status_code=422, detail="value is empty or contains whitespace")
    try:
        replaced = ctx.secrets.is_set(name)
        ctx.secrets.set(name, value)
    except KeychainUnavailable as exc:
        raise _unavailable(exc) from None
    with ctx.session_factory.begin() as session:
        write_audit(
            session,
            actor="user",
            action="secret.set",
            object_id=name,
            details={"replaced": replaced},
        )
    log.info("secret %s saved to the OS keychain", name)
    return {"name": name, "is_set": True}


@router.delete("/secrets/{name}")
def delete_secret(name: str, ctx: AppContext = Depends(get_ctx)) -> dict:
    _spec_or_404(name)
    try:
        removed = ctx.secrets.delete(name)
    except KeychainUnavailable as exc:
        raise _unavailable(exc) from None
    if removed:
        with ctx.session_factory.begin() as session:
            write_audit(session, actor="user", action="secret.delete", object_id=name)
        log.info("secret %s removed from the OS keychain", name)
    return {"name": name, "is_set": False}
