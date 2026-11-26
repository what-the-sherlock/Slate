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
    discord_service,
    discord_sync,
    email_sender,
    github_service,
    github_sync,
    ingest_service,
    mail_service,
    mail_sync,
    s3_blob_store,
    search_service,
)
from core.adapters.postgres.auth import PostgresSessionStore
from core.adapters.postgres.catalog import PostgresCatalog
from core.adapters.postgres.chat import PostgresChatStore
from core.adapters.postgres.engine import EngineKind, create_engine, create_session_factory
from core.adapters.postgres.health import PostgresHealthProbe
from core.adapters.postgres.jobs import PostgresJobQueue
from core.adapters.postgres.rate_limit import PostgresRateLimiter
from core.adapters.postgres.sources import PostgresSourceStore
from core.adapters.postgres.uow import UnitOfWork, make_uow
from core.adapters.postgres.workspaces import PostgresWorkspaceStore
from core.adapters.system import SystemClock
from core.config import Settings, get_settings
from core.ports.chat import ChatStore
from core.ports.system import HealthProbe
from core.services.answer import AnswerService
from core.services.auth import AuthService
from core.services.catalog import CatalogService
from core.services.discord import CONNECTOR as DISCORD
from core.services.discord import SYNC_DISCORD, DiscordService
from core.services.github import CONNECTOR as GITHUB
from core.services.github import SYNC_GITHUB, GitHubService
from core.services.inline import InlineRunner
from core.services.mail import IMAP, MBOX, SYNC_MAIL, MailService, MboxActions
from core.services.search import SearchService
from core.services.source_actions import ConnectorEntry, SourceActions
from core.services.uploads import PROCESS_UPLOAD, UploadService
from core.services.workspaces import WorkspaceService

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
    discord: DiscordService | None = None
    mail: MailService | None = None
    workspaces: WorkspaceService | None = None
    # Sync now / disconnect for any connected source; built from the fields above when the
    # caller doesn't pass one (tests).
    actions: SourceActions | None = None

    @property
    def workspaces_service(self) -> WorkspaceService:
        if self.workspaces is None:
            raise RuntimeError("Container.workspaces is not wired")
        return self.workspaces

    @property
    def source_actions(self) -> SourceActions:
        if self.actions is None:
            raise RuntimeError("Container.actions is not wired")
        return self.actions


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
    github, discord = github_service(settings, uow), discord_service(settings, uow)
    mail = mail_service(settings, uow)
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
        github=github,
        discord=discord,
        mail=mail,
        workspaces=WorkspaceService(
            PostgresWorkspaceStore(uow),
            PostgresSessionStore(uow),
            PostgresSourceStore(uow),
            PostgresRateLimiter(uow),
            SystemClock(),
            mailer=email_sender(settings),
            app_url=settings.app_url,
        ),
        actions=SourceActions(
            PostgresSourceStore(uow),
            {
                GITHUB: ConnectorEntry("GitHub", github, _github_inline(settings, uow)),
                DISCORD: ConnectorEntry("Discord", discord, _discord_inline(settings, uow)),
                IMAP: ConnectorEntry("Email", mail, _mail_inline(settings, uow)),
                MBOX: ConnectorEntry("Email import", MboxActions(mail)),
            },
        ),
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
    return InlineRunner(
        PostgresJobQueue(uow), sync.handle_inline, runner_id="api-inline",
        kinds=frozenset({SYNC_GITHUB}),
    )  # fmt: skip


def _discord_inline(settings: Settings, uow: UnitOfWork) -> InlineRunner | None:
    sync = discord_sync(settings, uow)
    if sync is None:
        return None
    return InlineRunner(
        PostgresJobQueue(uow), sync.handle_inline, runner_id="api-inline",
        kinds=frozenset({SYNC_DISCORD}),
    )  # fmt: skip


def _mail_inline(settings: Settings, uow: UnitOfWork) -> InlineRunner | None:
    sync = mail_sync(settings, uow)
    if sync is None:
        return None
    return InlineRunner(
        PostgresJobQueue(uow), sync.handle_inline, runner_id="api-inline",
        kinds=frozenset({SYNC_MAIL}),
    )  # fmt: skip


def _inline_runner(settings: Settings, uow: UnitOfWork) -> InlineRunner | None:
    ingest = ingest_service(settings, uow)
    if ingest is None:
        return None
    return InlineRunner(
        PostgresJobQueue(uow), ingest.process_upload, runner_id="api-inline",
        kinds=frozenset({PROCESS_UPLOAD}),
    )  # fmt: skip
