import asyncio
from datetime import timedelta
from uuid import uuid4

from core.adapters.postgres.rate_limit import PostgresRateLimiter
from core.adapters.postgres.uow import UnitOfWork
from core.ports.rate_limit import Limit


async def test_concurrent_spends_never_exceed_the_budget(uow: UnitOfWork) -> None:
    limiter = PostgresRateLimiter(uow)
    key = f"test:{uuid4()}"
    limit = Limit(budget=5, window=timedelta(minutes=5))
    results = await asyncio.gather(*(limiter.try_spend(key, 1, limit) for _ in range(12)))
    assert sum(results) == 5  # the advisory lock serialises check-and-record


async def test_weighted_costs(uow: UnitOfWork) -> None:
    limiter = PostgresRateLimiter(uow)
    key = f"test:{uuid4()}"
    limit = Limit(budget=100, window=timedelta(minutes=5))
    assert await limiter.try_spend(key, 60, limit)
    assert not await limiter.try_spend(key, 50, limit)  # would exceed: nothing recorded
    assert await limiter.try_spend(key, 40, limit)
