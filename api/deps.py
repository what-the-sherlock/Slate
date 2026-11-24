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
from core.adapters.postgres.jobs import PostgresJobQueue
from core.adapters.postgres.rate_limit import PostgresRateLimiter
from core.adapters.postgres.sources import PostgresSourceStore
from core.adapters.postgres.uow import UnitOfWork, make_uow
from core.adapters.s3.blob_store import S3BlobStore, S3Settings
from core.adapters.security.argon2_hasher import Argon2Hasher
from core.adapters.system import SystemClock
from core.config import Settings, get_settings
from core.ports.system import HealthProbe
from core.services.auth import AuthService
from core.services.uploads import UploadService

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True, slots=True)
class Container:
    settings: Settings
    health: HealthProbe
    expected_revision: str | None
    auth: AuthService
    uploads: UploadService | None = None  # None when storage isn't configured


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
        uploads=_upload_service(settings, uow),
    )


def _upload_service(settings: Settings, uow: UnitOfWork) -> UploadService | None:
    s = settings
    if not (
        s.aws_endpoint_url_s3
        and s.aws_access_key_id
        and s.aws_secret_access_key
        and s.session_secret
    ):
        return None
    blobs = S3BlobStore(
        S3Settings(
            endpoint_url=s.aws_endpoint_url_s3,
            region=s.aws_region or "us-east-1",
            access_key_id=s.aws_access_key_id,
            secret_access_key=s.aws_secret_access_key.get_secret_value(),
            bucket=s.uploads_bucket,
        )
    )
    return UploadService(
        blobs,
        PostgresSourceStore(uow),
        PostgresJobQueue(uow),
        PostgresRateLimiter(uow),
        SystemClock(),
        root_secret=s.session_secret.get_secret_value().encode(),
    )
