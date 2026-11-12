from __future__ import annotations

from fastapi import APIRouter, Depends

from engine.analytics.insights import analyse, collect
from engine.api.app import AppContext, get_ctx

router = APIRouter()


@router.get("/insights")
def insights(ctx: AppContext = Depends(get_ctx)) -> dict:
    """How published clips performed, and any threshold changes the numbers suggest."""
    with ctx.session_factory() as db:
        posts = collect(db)
    return analyse(posts, ctx.settings.get())
