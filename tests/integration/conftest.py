"""Integration fixtures: a real Neon *test* branch from DATABASE_URL(_DIRECT).

Tests are skipped (not failed) when no URL is configured, so contributors without a
Neon branch can still run the suite; CI always provides one.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

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
    return s


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
