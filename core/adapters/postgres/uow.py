"""Unit of work: one transaction per block, with a statement timeout.

    async with uow() as session:
        ...  # commit on success, rollback on any exception

``SET LOCAL`` scopes the timeout to this transaction, which is the only safe way to
set it through PgBouncer in transaction mode (startup ``options`` are not supported
by Neon's pooler).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

DEFAULT_STATEMENT_TIMEOUT_MS = 15_000

UnitOfWork = Callable[[], AbstractAsyncContextManager[AsyncSession]]


def make_uow(
    sessions: async_sessionmaker[AsyncSession],
    *,
    statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
) -> UnitOfWork:
    if statement_timeout_ms <= 0:
        raise ValueError("statement_timeout_ms must be positive")

    @asynccontextmanager
    async def uow() -> AsyncGenerator[AsyncSession]:
        async with sessions() as session, session.begin():
            # Integer is validated above; SET LOCAL can't take a bind parameter.
            await session.execute(
                text(f"SET LOCAL statement_timeout = {int(statement_timeout_ms)}")
            )
            yield session

    return uow
