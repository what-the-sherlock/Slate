from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool, QueuePool

from core.adapters.postgres.engine import (
    POOLED_MAX_OVERFLOW,
    POOLED_POOL_SIZE,
    POOLED_RECYCLE_S,
    EngineKind,
    create_engine,
)
from core.adapters.postgres.uow import make_uow
from core.request_metrics import record_connect, start_connect_stats

URL = "postgresql+psycopg://u:p@localhost:5432/db"


async def test_pooled_engine_keeps_a_small_recycled_pool() -> None:
    engine = create_engine(URL, EngineKind.POOLED)
    pool = engine.pool
    assert isinstance(pool, QueuePool)
    assert not isinstance(pool, NullPool)
    assert pool.size() == POOLED_POOL_SIZE
    assert pool._max_overflow == POOLED_MAX_OVERFLOW
    assert pool._recycle == POOLED_RECYCLE_S
    assert pool._pre_ping is False
    await engine.dispose()


async def test_direct_engine_uses_a_real_pool() -> None:
    engine = create_engine(URL, EngineKind.DIRECT)
    assert not isinstance(engine.pool, NullPool)
    await engine.dispose()


def test_make_uow_rejects_non_positive_timeout() -> None:
    with pytest.raises(ValueError, match="positive"):
        make_uow(None, statement_timeout_ms=0)  # type: ignore[arg-type]


def test_connect_stats_count_only_inside_a_request() -> None:
    record_connect(5.0)  # no request started in this context: ignored
    stats = start_connect_stats()
    record_connect(10.0)
    record_connect(2.5)
    assert (stats.count, stats.total_ms) == (2, 12.5)


# --- UoW retry on a dead pooled connection ------------------------------------


def _dead(invalidated: bool) -> DBAPIError:
    return DBAPIError("SET LOCAL", None, Exception("closed"), connection_invalidated=invalidated)


class FakeSession:
    def __init__(self, fail_with: BaseException | None) -> None:
        self.fail_with = fail_with
        self.events: list[str] = []

    async def begin(self) -> None:
        self.events.append("begin")

    async def execute(self, _stmt: Any) -> None:  # reason: SQL clause
        if self.fail_with is not None:
            raise self.fail_with
        self.events.append("set_timeout")

    async def commit(self) -> None:
        self.events.append("commit")

    async def rollback(self) -> None:
        self.events.append("rollback")

    async def close(self) -> None:
        self.events.append("close")


class FakeFactory:
    def __init__(self, *failures: BaseException | None) -> None:
        self.failures = list(failures)
        self.made: list[FakeSession] = []

    def __call__(self) -> FakeSession:
        session = FakeSession(self.failures.pop(0) if self.failures else None)
        self.made.append(session)
        return session


async def test_uow_commits_and_closes_on_success() -> None:
    factory = FakeFactory(None)
    async with make_uow(factory)():  # type: ignore[arg-type]
        pass
    assert factory.made[0].events == ["begin", "set_timeout", "commit", "close"]


async def test_uow_rolls_back_on_caller_error() -> None:
    factory = FakeFactory(None)
    with pytest.raises(RuntimeError):
        async with make_uow(factory)():  # type: ignore[arg-type]
            raise RuntimeError("boom")
    assert factory.made[0].events == ["begin", "set_timeout", "rollback", "close"]


async def test_uow_retries_once_on_a_dead_connection() -> None:
    factory = FakeFactory(_dead(invalidated=True), None)
    async with make_uow(factory)():  # type: ignore[arg-type]
        pass
    assert len(factory.made) == 2
    assert factory.made[0].events == ["begin", "close"]
    assert factory.made[1].events[-2:] == ["commit", "close"]


async def test_uow_gives_up_after_the_second_dead_connection() -> None:
    factory = FakeFactory(_dead(invalidated=True), _dead(invalidated=True))
    with pytest.raises(DBAPIError):
        async with make_uow(factory)():  # type: ignore[arg-type]
            pass
    assert len(factory.made) == 2


async def test_uow_does_not_retry_other_db_errors() -> None:
    factory = FakeFactory(_dead(invalidated=False))
    with pytest.raises(DBAPIError):
        async with make_uow(factory)():  # type: ignore[arg-type]
            pass
    assert len(factory.made) == 1
