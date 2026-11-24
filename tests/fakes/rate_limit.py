from __future__ import annotations

from datetime import datetime

from core.ports.rate_limit import Limit
from tests.fakes.system import FakeClock


class InMemoryRateLimiter:
    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock
        self.events: list[tuple[str, int, datetime]] = []

    async def try_spend(self, key: str, cost: int, limit: Limit) -> bool:
        cutoff = self._clock.now() - limit.window
        spent = sum(c for k, c, at in self.events if k == key and at > cutoff)
        if spent + cost > limit.budget:
            return False
        self.events.append((key, cost, self._clock.now()))
        return True
