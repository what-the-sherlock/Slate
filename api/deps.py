"""Composition root for the API: the only api/ module that constructs adapters.

Everything is built lazily on first use (no DB connection at import time) and cached
for the life of the process. Tests override ``get_container`` with fakes.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from core.adapters.factories import (
    answer_service,
    auth_service,
    github_service,
    github_sync,
    ingest_service,
    s3_blob_store,
    search_service,
)
from core.adapters.postgres.catalog import PostgresCatalog
from core.adapters.postgres.chat import PostgresChatStore
from core.adapters.postgres.engine import EngineKind, create_engine, create_session_factory
from core.adapters.postgres.health import PostgresHealthProbe
from core.adapters.postgres.jobs import PostgresJobQueue
from core.adapters.postgres.rate_limit import PostgresRateLimiter
from core.adapters.postgres.sources import PostgresSourceStore
from core.adapters.postgres.uow import UnitOfWork, make_uow
from core.adapters.system import SystemClock
from core.config import Settings, get_settings
from core.ports.chat import ChatStore
from core.ports.system import HealthProbe
from core.services.answer import AnswerService
from core.services.auth import AuthService
from core.services.catalog import CatalogService
from core.services.github import GitHubService
from core.services.inline import InlineRunner
from core.services.search import SearchService
from core.services.uploads import UploadService

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True, slots=True)
class Container:
    settings: Settings
    health: HealthProbe
    expected_revision: str | None
    auth: AuthService
    catalog: CatalogService
    uploads: UploadService | None = None  # None when storage isn't configured
    inline: InlineRunner | None = None  # None without storage + OpenAI key
    search: SearchService | None = None
    answers: AnswerService | None = None  # None without an OpenAI key
    chats: ChatStore | None = None
    github: GitHubService | None = None
    github_inline: InlineRunner | None = None  # runs a first/manual sync right away


@lru_cache(maxsize=1)
def expected_migration_head() -> str | None:
    """The newest migration shipped with this code (None before the first one)."""
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    return ScriptDirectory.from_config(cfg).get_current_head()


@lru_cache(maxsize=1)
def get_container() -> Container:
    settings = get_settings()
    settings.require_api_settings()
    engine = create_engine(settings.require_database_url(), EngineKind.POOLED)
    uow = make_uow(create_session_factory(engine))
    return Container(
        settings=settings,
        health=PostgresHealthProbe(uow),
        expected_revision=expected_migration_head(),
        auth=auth_service(settings, uow),
        catalog=CatalogService(PostgresCatalog(uow)),
        uploads=_upload_service(settings, uow),
        inline=_inline_runner(settings, uow),
        search=search_service(settings, uow),
        answers=answer_service(settings, uow),
        chats=PostgresChatStore(uow),
        github=github_service(settings, uow),
        github_inline=_github_inline(settings, uow),
    )


def _upload_service(settings: Settings, uow: UnitOfWork) -> UploadService | None:
    blobs = s3_blob_store(settings)
    if blobs is None or settings.session_secret is None:
        return None
    return UploadService(
        blobs,
        PostgresSourceStore(uow),
        PostgresJobQueue(uow),
        PostgresRateLimiter(uow),
        SystemClock(),
        root_secret=settings.session_secret.get_secret_value().encode(),
    )


def _github_inline(settings: Settings, uow: UnitOfWork) -> InlineRunner | None:
    sync = github_sync(settings, uow)
    if sync is None:
        return None
    return InlineRunner(PostgresJobQueue(uow), sync.handle_inline, runner_id="api-inline")


def _inline_runner(settings: Settings, uow: UnitOfWork) -> InlineRunner | None:
    ingest = ingest_service(settings, uow)
    if ingest is None:
        return None
    return InlineRunner(PostgresJobQueue(uow), ingest.process_upload, runner_id="api-inline")
