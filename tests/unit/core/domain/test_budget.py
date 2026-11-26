from __future__ import annotations

from decimal import Decimal

import pytest

from core.domain.budget import Spend, SpendLimits

LIMITS = SpendLimits(
    monthly_all=Decimal("5"),
    daily_all=Decimal("0.50"),
    monthly_workspace=Decimal("0.25"),
    daily_workspace=Decimal("0.05"),
)


def spend(
    month_all: str = "0", day_all: str = "0", month_ws: str = "0", day_ws: str = "0"
) -> Spend:
    return Spend(Decimal(month_all), Decimal(day_all), Decimal(month_ws), Decimal(day_ws))


def test_under_every_cap_is_allowed() -> None:
    assert LIMITS.refusal(spend("1", "0.1", "0.1", "0.01"), Decimal("0.01")) is None


@pytest.mark.parametrize(
    ("current", "fragment"),
    [
        (spend(month_all="4.995"), "budget for this month"),
        (spend(day_all="0.495"), "allowance for today is used up"),
        (spend(month_ws="0.245"), "this month's AI allowance"),
        (spend(day_ws="0.045"), "today's AI allowance"),
    ],
    ids=["global-month", "global-day", "workspace-month", "workspace-day"],
)
def test_each_cap_refuses_a_call_that_would_cross_it(current: Spend, fragment: str) -> None:
    reason = LIMITS.refusal(current, Decimal("0.01"))
    assert reason is not None
    assert fragment in reason


def test_the_global_day_cap_holds_even_when_every_workspace_is_under_its_own() -> None:
    # 20 fresh accounts each spent 0.025 today: each is fine, together they're at the cap.
    current = spend(month_all="0.50", day_all="0.50", month_ws="0.025", day_ws="0.025")
    assert LIMITS.refusal(current, Decimal("0.001")) is not None


def test_global_limits_are_reported_before_workspace_ones() -> None:
    reason = LIMITS.refusal(spend("5", "0.5", "0.25", "0.05"), Decimal("0.01"))
    assert reason is not None
    assert reason.startswith("Slate's")


def test_monthly_only_keeps_the_old_single_cap() -> None:
    limits = SpendLimits.monthly_only(Decimal("5"))
    assert limits.refusal(spend("4.98", "4.98", "4.98", "4.98"), Decimal("0.01")) is None
    assert limits.refusal(spend("5"), Decimal("0.01")) is not None


def test_near_global_limit_at_80_percent() -> None:
    assert not LIMITS.near_global_limit(spend("3.99"))
    assert LIMITS.near_global_limit(spend("4"))


# ------------------------------------------------- owner exemption + new-account ramp (M7)


def _limits() -> SpendLimits:
    return SpendLimits(
        monthly_all=Decimal(5), daily_all=Decimal("0.50"), monthly_workspace=Decimal("0.25"),
        daily_workspace=Decimal("0.05"), new_account_daily=Decimal("0.01"),
    )  # fmt: skip


def _spend(**kw: object) -> Spend:
    base = {"month_all": Decimal(0), "day_all": Decimal(0), "month_workspace": Decimal(0),
            "day_workspace": Decimal(0)}  # fmt: skip
    return Spend(**{**base, **kw})


def test_strangers_exhausting_the_day_cannot_lock_the_owner_out() -> None:
    burned = _spend(day_all=Decimal("0.50"))
    assert _limits().refusal(burned, Decimal("0.001")) is not None  # everyone else waits
    owner = _spend(day_all=Decimal("0.50"), privileged=True, day_workspace=Decimal("3"))
    assert _limits().refusal(owner, Decimal("0.001")) is None


def test_the_monthly_hard_stop_binds_the_owner_too() -> None:
    owner = _spend(month_all=Decimal(5), privileged=True)
    assert _limits().refusal(owner, Decimal("0.001")) is not None


def test_new_workspaces_get_a_smaller_daily_allowance() -> None:
    new = _spend(day_workspace=Decimal("0.01"), workspace_age_days=2)
    reason = _limits().refusal(new, Decimal("0.001"))
    assert reason is not None
    assert "first week" in reason
    old = _spend(day_workspace=Decimal("0.01"), workspace_age_days=30)
    assert _limits().refusal(old, Decimal("0.001")) is None
    assert _limits().daily_for(new) == Decimal("0.01")
    assert _limits().daily_for(old) == Decimal("0.05")
