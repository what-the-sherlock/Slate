"""HealthProbe over Postgres: liveness of the DB and the applied Alembic revision."""

from __future__ import annotations

import asyncio

import structlog
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError, ProgrammingError

from core.adapters.postgres.uow import UnitOfWork

log = structlog.get_logger(__name__)
PROBE_TIMEOUT_S = 5.0


class PostgresHealthProbe:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def database_ok(self) -> bool:
        try:
            async with asyncio.timeout(PROBE_TIMEOUT_S), self._uow() as s:
                return (await s.execute(text("SELECT 1"))).scalar_one() == 1
        except (TimeoutError, OperationalError, DBAPIError) as e:
            log.warning("db_health_failed", error_type=type(e).__name__)
            return False

    async def migration_revision(self) -> str | None:
        try:
            async with asyncio.timeout(PROBE_TIMEOUT_S), self._uow() as s:
                row = (await s.execute(text("SELECT version_num FROM alembic_version"))).first()
        except ProgrammingError:  # table doesn't exist: never migrated
            return None
        return None if row is None else str(row[0])
