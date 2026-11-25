"""Spend limits for paid AI calls (ARCHITECTURE §12, ADR-0021). Pure; no I/O.

Once anyone can sign up, one monthly cap isn't enough: a burst of accounts could use the
whole month in a day, and one heavy user could crowd everyone else out. So four caps,
all checked before every paid call:

* global per month (MONTHLY_LLM_BUDGET_USD, the hard stop, ~$5)
* global per day (a burst can use at most one day's share)
* per workspace per month, and per workspace per day (one user can't starve the rest)

Days and months are UTC calendar periods, matching ``usage_ledger.day``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

UNLIMITED = Decimal("1e9")


@dataclass(frozen=True, slots=True)
class Spend:
    """What has been spent so far (USD), globally and by one workspace."""

    month_all: Decimal
    day_all: Decimal
    month_workspace: Decimal
    day_workspace: Decimal


@dataclass(frozen=True, slots=True)
class SpendLimits:
    monthly_all: Decimal
    daily_all: Decimal = UNLIMITED
    monthly_workspace: Decimal = UNLIMITED
    daily_workspace: Decimal = UNLIMITED

    @classmethod
    def monthly_only(cls, monthly_all: Decimal) -> SpendLimits:
        return cls(monthly_all=monthly_all)

    def refusal(self, spend: Spend, upcoming: Decimal = Decimal(0)) -> str | None:
        """Why a call costing ``upcoming`` may not run now, or None if it may.

        Global limits are reported first: they're the ones a user can't fix by waiting
        for their own allowance.
        """
        if spend.month_all + upcoming > self.monthly_all:
            return "Slate's AI budget for this month is used up. It resets on the 1st."
        if spend.day_all + upcoming > self.daily_all:
            return "Slate's AI allowance for today is used up. It resets at midnight UTC."
        if spend.month_workspace + upcoming > self.monthly_workspace:
            return "You've used this month's AI allowance. It resets on the 1st."
        if spend.day_workspace + upcoming > self.daily_workspace:
            return "You've used today's AI allowance. It resets at midnight UTC."
        return None

    def near_global_limit(self, spend: Spend) -> bool:
        """At 80% of the monthly hard stop: worth a warning in the logs."""
        return spend.month_all >= self.monthly_all * Decimal("0.8")
