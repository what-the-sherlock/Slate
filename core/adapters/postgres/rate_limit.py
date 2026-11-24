"""RateLimiter over Postgres: a per-key advisory lock makes check-and-record atomic."""

from __future__ import annotations

from sqlalchemy import text

from core.adapters.postgres.uow import UnitOfWork
from core.ports.rate_limit import Limit


class PostgresRateLimiter:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def try_spend(self, key: str, cost: int, limit: Limit) -> bool:
        async with self._uow() as s:
            await s.execute(
                text("SELECT pg_advisory_xact_lock(hashtext('rate:' || :k))"), {"k": key}
            )
            spent = (
                await s.execute(
                    text(
                        "SELECT coalesce(sum(cost), 0) FROM rate_events"
                        " WHERE key = :k AND created_at > now() - make_interval(secs => :w)"
                    ),
                    {"k": key, "w": limit.window.total_seconds()},
                )
            ).scalar_one()
            if int(spent) + cost > limit.budget:
                return False
            await s.execute(
                text("INSERT INTO rate_events (key, cost) VALUES (:k, :c)"), {"k": key, "c": cost}
            )
            await s.execute(
                text("DELETE FROM rate_events WHERE created_at < now() - interval '2 days'")
            )
            return True
