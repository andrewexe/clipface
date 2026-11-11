"""The engine's HTTP API. Loopback only; every route requires the per-session token."""

from __future__ import annotations

import asyncio
import hmac
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
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
from engine.monitor.service import Monitor
from engine.recorder.service import Recorder
from engine.render.pipeline import ClipPipeline
from engine.settings import SettingsStore
from engine.signals.service import SignalService

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
    monitor: Monitor | None = None
    recorder: Recorder | None = None
    signals: SignalService | None = None
    pipeline: ClipPipeline | None = None
    settings: SettingsStore = field(init=False)
    session_factory: sessionmaker[Session] = field(init=False)
    # Read once: the schema only changes at startup, and health is polled constantly.
    schema_revision: str | None = field(init=False)
    started_at: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        self.session_factory = make_session_factory(self.db_engine)
        self.schema_revision = current_revision(self.db_engine)
        self.settings = SettingsStore(self.config.settings_file)

    def services(self) -> list:
        """Background services, in start order."""
        return [self.monitor, self.recorder, self.signals, self.pipeline]


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
    from engine.api import health, logs, moments, settings, streamers

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # Background services run inside the server's event loop for its whole life.
        services = [svc for svc in ctx.services() if svc is not None]
        tasks = [asyncio.create_task(svc.run()) for svc in services]
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for svc in services:
                await svc.aclose()

    app = FastAPI(
        lifespan=lifespan,
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
        allow_methods=["GET", "PUT", "POST", "PATCH", "DELETE"],
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
    app.include_router(streamers.router)
    app.include_router(moments.router)
    return app
