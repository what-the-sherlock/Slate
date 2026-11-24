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

from core.adapters.postgres.auth import (
    PostgresAccountStore,
    PostgresLoginThrottle,
    PostgresSessionStore,
)
from core.adapters.postgres.engine import EngineKind, create_engine, create_session_factory
from core.adapters.postgres.health import PostgresHealthProbe
from core.adapters.postgres.uow import make_uow
from core.adapters.security.argon2_hasher import Argon2Hasher
from core.adapters.system import SystemClock
from core.config import Settings, get_settings
from core.ports.system import HealthProbe
from core.services.auth import AuthService

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True, slots=True)
class Container:
    settings: Settings
    health: HealthProbe
    expected_revision: str | None
    auth: AuthService


@lru_cache(maxsize=1)
def expected_migration_head() -> str | None:
    """The newest migration shipped with this code (None before the first one)."""
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    return ScriptDirectory.from_config(cfg).get_current_head()


@lru_cache(maxsize=1)
def get_container() -> Container:
    settings = get_settings()
    engine = create_engine(settings.require_database_url(), EngineKind.POOLED)
    uow = make_uow(create_session_factory(engine))
    return Container(
        settings=settings,
        health=PostgresHealthProbe(uow),
        expected_revision=expected_migration_head(),
        auth=AuthService(
            PostgresAccountStore(uow),
            PostgresSessionStore(uow),
            PostgresLoginThrottle(uow),
            Argon2Hasher(),
            SystemClock(),
            setup_token=settings.setup_token.get_secret_value() if settings.setup_token else None,
            setup_token_required=settings.env == "prod",
        ),
    )
