"""Integration fixtures: a real Neon *test* branch from DATABASE_URL(_DIRECT).

Tests are skipped (not failed) when no URL is configured, so contributors without a
Neon branch can still run the suite; CI always provides one.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from urllib.parse import urlsplit

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from core.adapters.postgres.engine import EngineKind, create_engine, create_session_factory
from core.adapters.postgres.uow import UnitOfWork, make_uow
from core.config import Settings


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "tests/integration" in item.path.as_posix() or "tests\\integration" in str(item.path):
            item.add_marker(pytest.mark.integration)


@pytest.fixture(scope="session")
def settings() -> Settings:
    s = Settings()
    url = s.database_url_direct or s.database_url
    if url is None:
        pytest.skip("DATABASE_URL(_DIRECT) not set; integration tests need a Neon test branch")
    if s.env == "prod":
        pytest.exit("refusing to run integration tests with ENV=prod", returncode=2)
    pooled, direct = s.database_url, s.database_url_direct
    if (
        pooled
        and direct
        and _endpoint(pooled.get_secret_value()) != _endpoint(direct.get_secret_value())
    ):
        # e.g. `neon deploy` rewrote DATABASE_URL to the production branch while
        # DATABASE_URL_DIRECT still points at the test branch.
        pytest.exit(
            "DATABASE_URL and DATABASE_URL_DIRECT point at different Neon endpoints; "
            "both must be the *test* branch",
            returncode=2,
        )
    return s


def _endpoint(url: str) -> str:
    """Neon endpoint id from a URL host: ep-xxx(-pooler).region.aws.neon.tech -> ep-xxx."""
    host = urlsplit(url.replace("postgresql+psycopg://", "postgresql://")).hostname or ""
    return host.split(".", 1)[0].removesuffix("-pooler")


@pytest.fixture(scope="session")
async def direct_engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(settings.require_database_url(direct=True), EngineKind.DIRECT)
    yield engine
    await engine.dispose()


@pytest.fixture(scope="session")
async def pooled_engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(settings.require_database_url(), EngineKind.POOLED)
    yield engine
    await engine.dispose()


@pytest.fixture
def uow(direct_engine: AsyncEngine) -> UnitOfWork:
    return make_uow(create_session_factory(direct_engine))
