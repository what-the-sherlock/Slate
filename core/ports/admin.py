"""Ports for the platform admin console (ADR-0042): the auth event log and admin reads."""

from __future__ import annotations

from datetime import date, datetime
from typing import Protocol

from core.domain.admin import (
    AdminUser,
    AuthEvent,
    AuthEventKind,
    Bucket,
    DeleteOutcome,
    Overview,
    UserSort,
    UserStatus,
)
from core.domain.ids import UserId


class AuthEventLog(Protocol):
    async def record(
        self, kind: AuthEventKind, user_id: UserId | None, *, actor_id: UserId | None = None
    ) -> None:
        """Append one event. Carries no IP, user agent or email by design."""
        ...

    async def delete_before(self, cutoff: datetime) -> int:
        """Retention: delete events older than ``cutoff``; returns how many."""
        ...


class AdminStore(Protocol):
    """Platform-wide reads. Only AdminService calls this, after its admin check."""

    async def overview(self, now: datetime) -> Overview: ...

    async def signup_counts(self, since: date, bucket: Bucket) -> dict[date, int]:
        """Sign-ups per bucket start (UTC), for buckets with at least one."""
        ...

    async def failed_login_counts(self, since: date) -> dict[date, int]: ...

    async def list_users(
        self,
        *,
        query: str,
        status: UserStatus,
        sort: UserSort,
        descending: bool,
        offset: int,
        limit: int,
        now: datetime,
    ) -> tuple[list[AdminUser], int]:
        """One page of accounts and the total matching the filters."""
        ...

    async def get_user(self, user_id: UserId) -> AdminUser | None: ...

    async def list_events(
        self, *, kind: AuthEventKind | None, before_id: int | None, limit: int
    ) -> list[AuthEvent]:
        """Newest first; keyset pagination on id."""
        ...

    async def delete_user(self, user_id: UserId, *, actor_id: UserId) -> DeleteOutcome:
        """Delete the account and everything it owns, in one transaction: its sole-member
        workspaces (all content cascades), what it connected in workspaces it joined, and
        then the user row (sessions, codes and connections cascade). Records
        ``account_deleted``. Refuses, changing nothing, if it owns a workspace other
        people belong to. Uploaded files left without a document are removed by the
        worker's abandoned-upload sweep."""
        ...

    async def set_disabled(self, user_id: UserId, disabled: bool, *, actor_id: UserId) -> bool:
        """Set or clear ``disabled_at`` and record the matching auth event, atomically.
        True only if the status actually changed (False if unchanged or no such account)."""
        ...
