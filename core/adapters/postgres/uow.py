"""Unit of work: one transaction per block, with a statement timeout.

    async with uow() as session:
        ...  # commit on success, rollback on any exception

``SET LOCAL`` scopes the timeout to this transaction, which is the only safe way to
set it through PgBouncer in transaction mode (startup ``options`` are not supported
by Neon's pooler).

Pooled connections can die while idle (Neon suspends compute, a Vercel instance is
frozen). ``SET LOCAL`` is the first statement of every block and no caller work has
run yet, so if it finds the connection dead we discard it and retry once on a fresh
one. This replaces ``pool_pre_ping``, which would cost a round trip on every block.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
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
    # Integer is validated above; SET LOCAL can't take a bind parameter.
    set_timeout = text(f"SET LOCAL statement_timeout = {int(statement_timeout_ms)}")

    async def _open() -> AsyncSession:
        """A session inside a begun transaction, retrying once on a dead connection."""
        for attempt in (1, 2):
            session = sessions()
            try:
                await session.begin()
                await session.execute(set_timeout)
            except DBAPIError as exc:
                await session.close()
                if attempt == 1 and exc.connection_invalidated:
                    continue
                raise
            except BaseException:
                await session.close()
                raise
            return session
        raise AssertionError("unreachable")  # pragma: no cover

    @asynccontextmanager
    async def uow() -> AsyncGenerator[AsyncSession]:
        session = await _open()
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
        finally:
            await session.close()

    return uow
