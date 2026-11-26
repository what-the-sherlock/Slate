"""Spend limits for paid AI calls (ARCHITECTURE §12, ADR-0021). Pure; no I/O.

Once anyone can sign up, one monthly cap isn't enough: a burst of accounts could use the
whole month in a day, and one heavy user could crowd everyone else out. So four caps,
all checked before every paid call:

* global per month (MONTHLY_LLM_BUDGET_USD, the hard stop, ~$5)
* global per day (a burst can use at most one day's share)
* per workspace per month, and per workspace per day (one user can't starve the rest)

Two refinements (audit, M7): the platform owner's workspace is exempt from the global
*daily* cap, so a burst of strangers can't lock the owner out (the global monthly hard stop
still applies to everyone); and workspaces younger than ``new_account_days`` get a smaller
daily allowance, so a sign-up burst burns far less.

Days and months are UTC calendar periods, matching ``usage_ledger.day``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

UNLIMITED = Decimal("1e9")


@dataclass(frozen=True, slots=True)
class Spend:
    """What has been spent so far (USD), globally and by one workspace."""

    month_all: Decimal
    day_all: Decimal
    month_workspace: Decimal
    day_workspace: Decimal
    privileged: bool = False  # the platform owner's workspace (OWNER_EMAIL, verified)
    workspace_age_days: int = 10**6  # unknown counts as established


@dataclass(frozen=True, slots=True)
class SpendLimits:
    monthly_all: Decimal
    daily_all: Decimal = UNLIMITED
    monthly_workspace: Decimal = UNLIMITED
    daily_workspace: Decimal = UNLIMITED
    new_account_daily: Decimal | None = None  # daily allowance while the workspace is new
    new_account_days: int = 7

    @classmethod
    def monthly_only(cls, monthly_all: Decimal) -> SpendLimits:
        return cls(monthly_all=monthly_all)

    def refusal(
        self, spend: Spend, upcoming: Decimal = Decimal(0), *, privileged: bool | None = None
    ) -> str | None:
        """Why a call costing ``upcoming`` may not run now, or None if it may.

        Global limits are reported first: they're the ones a user can't fix by waiting
        for their own allowance.
        """
        # Who is spending decides the exemption when known (chat, search: the owner account
        # itself); background work falls back to the workspace's own flag.
        spend = replace(spend, privileged=spend.privileged if privileged is None else privileged)
        if spend.month_all + upcoming > self.monthly_all:
            return "Slate's AI budget for this month is used up. It resets on the 1st."
        if not spend.privileged and spend.day_all + upcoming > self.daily_all:
            return "Slate's AI allowance for today is used up. It resets at midnight UTC."
        if spend.privileged:
            return None  # the owner is bound by the monthly hard stop only
        if spend.month_workspace + upcoming > self.monthly_workspace:
            return "You've used this month's AI allowance. It resets on the 1st."
        if spend.day_workspace + upcoming > self.daily_for(spend):
            if self.daily_for(spend) < self.daily_workspace:
                return (
                    "New accounts have a smaller daily AI allowance for their first week. "
                    "It resets at midnight UTC."
                )
            return "You've used today's AI allowance. It resets at midnight UTC."
        return None

    def daily_for(self, spend: Spend) -> Decimal:
        """This workspace's daily allowance: smaller while it's new."""
        if self.new_account_daily is not None and spend.workspace_age_days < self.new_account_days:
            return min(self.new_account_daily, self.daily_workspace)
        return self.daily_workspace

    def near_global_limit(self, spend: Spend) -> bool:
        """At 80% of the monthly hard stop: worth a warning in the logs."""
        return spend.month_all >= self.monthly_all * Decimal("0.8")
