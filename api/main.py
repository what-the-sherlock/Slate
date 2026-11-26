"""FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI

from api.errors import install_error_handlers
from api.middleware import RequestIdMiddleware
from api.routers import (
    auth,
    chat,
    discord,
    github,
    health,
    mail,
    search,
    sources,
    uploads,
    workspaces,
)
from core.config import get_settings
from core.logs import configure_logging

API_PREFIX = "/api/v1"


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, json=settings.env != "dev")
    app = FastAPI(
        title="Slate API",
        version=health.VERSION,
        summary="Permission-aware knowledge search with cited answers.",
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url=f"{API_PREFIX}/docs",
        redoc_url=None,
    )
    install_error_handlers(app)
    app.include_router(health.router, prefix=API_PREFIX)
    app.include_router(auth.router, prefix=API_PREFIX)
    app.include_router(uploads.router, prefix=API_PREFIX)
    app.include_router(sources.router, prefix=API_PREFIX)
    app.include_router(search.router, prefix=API_PREFIX)
    app.include_router(chat.router, prefix=API_PREFIX)
    app.include_router(github.router, prefix=API_PREFIX)
    app.include_router(discord.router, prefix=API_PREFIX)
    app.include_router(mail.router, prefix=API_PREFIX)
    app.include_router(workspaces.router, prefix=API_PREFIX)
    app.add_middleware(RequestIdMiddleware)
    return app


app = create_app()
