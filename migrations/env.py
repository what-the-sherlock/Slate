"""Alembic environment: async migrations over the DIRECT (non-pooled) Neon URL.

Migrations need a real session (DDL, advisory locks, ``CREATE INDEX CONCURRENTLY``),
which PgBouncer's transaction mode can't give, so the pooled URL is never used here.
Schema is written by hand in each revision (no autogenerate), because the search
SQL, partial indexes and generated columns are easier to review as explicit DDL.
"""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy.engine import Connection

from core.adapters.postgres.engine import (
    EngineKind,
    create_engine,
    use_selector_loop_on_windows,
)
from core.config import get_settings

config = context.config


def _run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=None, transaction_per_migration=True)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async() -> None:
    engine = create_engine(get_settings().require_database_url(direct=True), EngineKind.DIRECT)
    try:
        async with engine.connect() as conn:
            await conn.run_sync(_run)
            await conn.commit()
    finally:
        await engine.dispose()


if context.is_offline_mode():
    context.configure(url="postgresql+psycopg://", literal_binds=True, target_metadata=None)
    with context.begin_transaction():
        context.run_migrations()
else:
    use_selector_loop_on_windows()
    asyncio.run(_run_async())
