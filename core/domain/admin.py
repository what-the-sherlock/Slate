"""Platform admin console: auth event kinds and read models (pure; no I/O, ADR-0042).

The console shows operational facts about accounts, never content: no documents, chats,
connections, password hashes, tokens, IPs or user agents.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum

from core.domain.ids import UserId

AUTH_EVENT_RETENTION = timedelta(days=90)
ACTIVE_WINDOW = timedelta(days=30)  # "active" = signed in or used the app in 30 days
MAX_PAGE = 50


class AuthEventKind(StrEnum):
    SIGNUP = "signup"
    EMAIL_VERIFIED = "email_verified"
    LOGIN_SUCCESS = "login_success"
    LOGIN_FAILED = "login_failed"
    LOGIN_THROTTLED = "login_throttled"
    LOGOUT = "logout"
    PASSWORD_RESET = "password_reset"  # noqa: S105 (an event name, not a secret)
    ACCOUNT_DISABLED = "account_disabled"
    ACCOUNT_ENABLED = "account_enabled"
    ACCOUNT_DELETED = "account_deleted"


class DeleteOutcome(StrEnum):
    DELETED = "deleted"
    NOT_FOUND = "not_found"
    OWNS_SHARED_WORKSPACE = "owns_shared_workspace"


class UserStatus(StrEnum):
    ALL = "all"
    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    DISABLED = "disabled"
    ACTIVE = "active"
    INACTIVE = "inactive"


class UserSort(StrEnum):
    JOINED = "joined"
    LAST_ACTIVE = "last_active"
    EMAIL = "email"


class Bucket(StrEnum):
    DAY = "day"
    WEEK = "week"
    MONTH = "month"


@dataclass(frozen=True, slots=True)
class Overview:
    """``verified``, ``unverified`` and ``disabled`` partition ``total``: the first two count
    enabled accounts only, so the three always add up."""

    total: int
    verified: int
    unverified: int
    disabled: int
    new_today: int
    new_7d: int
    new_30d: int
    new_prev_7d: int  # the 7 days before, for the trend line
    active_7d: int
    active_30d: int
    failed_logins_24h: int


@dataclass(frozen=True, slots=True)
class SeriesPoint:
    start: date
    count: int


@dataclass(frozen=True, slots=True)
class AdminUser:
    id: UserId
    email: str
    name: str
    joined_at: datetime
    email_verified: bool
    disabled: bool
    last_active_at: datetime | None
    workspaces: int


@dataclass(frozen=True, slots=True)
class AuthEvent:
    id: int
    kind: AuthEventKind
    at: datetime
    user_id: UserId | None
    user_email: str | None  # joined at read time, never stored on the event


@dataclass(frozen=True, slots=True)
class Page[T]:
    items: tuple[T, ...]
    next_cursor: str | None
    total: int | None = None


def fill_series(
    counts: dict[date, int], start: date, end: date, bucket: Bucket
) -> list[SeriesPoint]:
    """Every bucket from start to end inclusive, with 0 where nothing happened (a chart
    with gaps would read as missing data)."""
    points: list[SeriesPoint] = []
    cur = bucket_start(start, bucket)
    while cur <= end:
        points.append(SeriesPoint(cur, counts.get(cur, 0)))
        cur = _next(cur, bucket)
    return points


def bucket_start(d: date, bucket: Bucket) -> date:
    if bucket is Bucket.DAY:
        return d
    if bucket is Bucket.WEEK:
        return d - timedelta(days=d.weekday())  # ISO weeks start Monday, like date_trunc
    return d.replace(day=1)


def _next(d: date, bucket: Bucket) -> date:
    if bucket is Bucket.DAY:
        return d + timedelta(days=1)
    if bucket is Bucket.WEEK:
        return d + timedelta(days=7)
    return date(d.year + (d.month == 12), d.month % 12 + 1, 1)
