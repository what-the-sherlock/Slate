"""A sliding-window rate limiter port (counts or weighted costs, e.g. bytes)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol


@dataclass(frozen=True, slots=True)
class Limit:
    budget: int  # maximum total cost inside the window
    window: timedelta


class RateLimiter(Protocol):
    async def try_spend(self, key: str, cost: int, limit: Limit) -> bool:
        """Atomically record ``cost`` against ``key`` if it fits the limit.

        Returns False (recording nothing) when the spend would exceed the budget.
        """
        ...
