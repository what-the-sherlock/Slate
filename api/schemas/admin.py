"""Admin console responses (ADR-0042). These models are the allow-list of what leaves the
server: no password hashes, tokens, IPs, user agents or content, ever."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

EventKind = Literal[
    "signup", "email_verified", "login_success", "login_failed", "login_throttled",
    "logout", "password_reset", "account_disabled", "account_enabled", "account_deleted",
]  # fmt: skip


class OverviewOut(BaseModel):
    total: int
    verified: int
    unverified: int
    disabled: int
    new_today: int
    new_7d: int
    new_30d: int
    new_prev_7d: int
    active_7d: int
    active_30d: int
    failed_logins_24h: int


class SeriesPointOut(BaseModel):
    start: date
    count: int


class SeriesOut(BaseModel):
    bucket: Literal["day", "week", "month"]
    points: list[SeriesPointOut]


class AdminUserOut(BaseModel):
    id: UUID
    email: str
    name: str
    joined_at: datetime
    email_verified: bool
    disabled: bool
    last_active_at: datetime | None
    workspaces: int
    you: bool


class UserPageOut(BaseModel):
    items: list[AdminUserOut]
    next_cursor: str | None
    total: int


class AuthEventOut(BaseModel):
    id: int
    kind: EventKind
    at: datetime
    user_id: UUID | None
    user_email: str | None


class EventPageOut(BaseModel):
    items: list[AuthEventOut]
    next_cursor: str | None


class DeleteUserBody(BaseModel):
    confirm_email: str = Field(min_length=3, max_length=320)


class UserStatusBody(BaseModel):
    disabled: bool
