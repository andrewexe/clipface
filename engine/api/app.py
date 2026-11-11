"""The engine's HTTP API. Loopback only; every route requires the per-session token."""

from __future__ import annotations

import hmac
import time
from dataclasses import dataclass, field

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker
from starlette.middleware.trustedhost import TrustedHostMiddleware

from engine import __version__
from engine.config import Config
from engine.db.session import current_revision, make_session_factory
from engine.keychain import SecretStore

# The Tauri webview (Windows, then macOS/Linux) and the Vite dev server.
ALLOWED_ORIGINS = [
    "http://tauri.localhost",
    "https://tauri.localhost",
    "tauri://localhost",
    "http://localhost:1420",
]


@dataclass
class AppContext:
    config: Config
    token: str
    db_engine: Engine
    secrets: SecretStore
    session_factory: sessionmaker[Session] = field(init=False)
    # Read once: the schema only changes at startup, and health is polled constantly.
    schema_revision: str | None = field(init=False)
    started_at: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        self.session_factory = make_session_factory(self.db_engine)
        self.schema_revision = current_revision(self.db_engine)


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def require_token(request: Request) -> None:
    ctx: AppContext = request.app.state.ctx
    scheme, _, supplied = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(supplied.encode(), ctx.token.encode()):
        raise HTTPException(
            status_code=401,
            detail="missing or invalid session token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def create_app(ctx: AppContext) -> FastAPI:
    from engine.api import health, logs, settings

    app = FastAPI(
        title="Clipper engine",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        dependencies=[Depends(require_token)],
    )
    app.state.ctx = ctx

    # Rejects requests whose Host is not loopback (DNS rebinding). Added first so CORS wraps it.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOWED_ORIGINS,
        allow_methods=["GET", "PUT", "POST", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI's default echoes the rejected input, which here could be a secret.
        errors = [
            {"loc": list(err["loc"]), "msg": err["msg"], "type": err["type"]}
            for err in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": errors})

    app.include_router(health.router)
    app.include_router(settings.router)
    app.include_router(logs.router)
    return app
