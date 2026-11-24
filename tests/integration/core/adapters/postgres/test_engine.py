import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from core.adapters.postgres.engine import create_session_factory
from core.adapters.postgres.health import PostgresHealthProbe
from core.adapters.postgres.uow import UnitOfWork, make_uow


async def test_direct_engine_connects(uow: UnitOfWork) -> None:
    async with uow() as s:
        assert (await s.execute(text("SELECT 1"))).scalar_one() == 1


async def test_pooled_engine_survives_repeated_statements_through_pgbouncer(
    pooled_engine: AsyncEngine,
) -> None:
    # With prepared statements enabled, psycopg would PREPARE after 5 executions and
    # PgBouncer (transaction mode) would fail with "prepared statement does not exist".
    pooled_uow = make_uow(create_session_factory(pooled_engine))
    for i in range(12):
        async with pooled_uow() as s:
            assert (await s.execute(text("SELECT CAST(:i AS int)"), {"i": i})).scalar_one() == i


async def test_uow_rolls_back_on_error(uow: UnitOfWork) -> None:
    async def create_then_fail() -> None:
        async with uow() as s:
            await s.execute(text("CREATE TABLE uow_rollback_probe (n int)"))
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await create_then_fail()

    async with uow() as s:
        exists = (await s.execute(text("SELECT to_regclass('uow_rollback_probe')"))).scalar_one()
    assert exists is None


async def test_uow_enforces_statement_timeout(direct_engine: AsyncEngine) -> None:
    fast_uow = make_uow(create_session_factory(direct_engine), statement_timeout_ms=200)
    with pytest.raises(DBAPIError, match="statement timeout"):
        async with fast_uow() as s:
            await s.execute(text("SELECT pg_sleep(2)"))


async def test_health_probe_reports_db_ok(uow: UnitOfWork) -> None:
    assert await PostgresHealthProbe(uow).database_ok()


async def test_pgvector_extension_is_available(uow: UnitOfWork) -> None:
    async with uow() as s:
        row = (
            await s.execute(
                text("SELECT default_version FROM pg_available_extensions WHERE name = 'vector'")
            )
        ).first()
    assert row is not None
