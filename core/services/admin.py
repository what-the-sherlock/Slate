"""Platform admin console (ADR-0042): account metrics, the user list, security events, and
disabling accounts.

Who is an admin is decided only by the server: ``Identity.is_platform_admin`` is computed
from the session's *verified* email matching OWNER_EMAIL (AuthService._is_owner). Nothing
stored, nothing a client can send, can grant it. Every method checks it first and answers
NotFound otherwise, so the console doesn't reveal that it exists.

Being the platform admin grants no access to anyone's documents, chats or sources: this
service reads account metadata only (core/domain/admin.py lists what it never returns).
"""

from __future__ import annotations

from datetime import timedelta

import structlog

from core.domain.admin import (
    AUTH_EVENT_RETENTION,
    MAX_PAGE,
    AdminUser,
    AuthEvent,
    AuthEventKind,
    Bucket,
    Overview,
    Page,
    SeriesPoint,
    UserSort,
    UserStatus,
    bucket_start,
    fill_series,
)
from core.domain.auth import Identity, canonical_email
from core.domain.errors import NotFound, PermissionDenied, ValidationFailed
from core.domain.ids import UserId
from core.ports.admin import AdminStore, AuthEventLog
from core.ports.auth import SessionStore
from core.ports.system import Clock

log = structlog.get_logger("admin")

NOT_FOUND = "Not found."
RANGES = {30: Bucket.DAY, 90: Bucket.WEEK, 365: Bucket.MONTH}  # days → default bucket
MAX_QUERY = 100


class AdminService:
    def __init__(
        self,
        store: AdminStore,
        events: AuthEventLog,
        sessions: SessionStore,
        clock: Clock,
        *,
        owner_email: str | None,
    ) -> None:
        self._store = store
        self._events = events
        self._sessions = sessions
        self._clock = clock
        self._owner = canonical_email(owner_email) if owner_email else None

    async def overview(self, identity: Identity) -> Overview:
        _require_admin(identity)
        return await self._store.overview(self._clock.now())

    async def signups(
        self, identity: Identity, *, days: int, bucket: Bucket | None = None
    ) -> list[SeriesPoint]:
        _require_admin(identity)
        if days not in RANGES:
            raise ValidationFailed("Choose 30, 90 or 365 days.")
        bucket = bucket or RANGES[days]
        today = self._clock.now().date()
        since = bucket_start(today - timedelta(days=days - 1), bucket)
        return fill_series(await self._store.signup_counts(since, bucket), since, today, bucket)

    async def failed_logins(self, identity: Identity, *, days: int = 30) -> list[SeriesPoint]:
        _require_admin(identity)
        days = max(1, min(days, AUTH_EVENT_RETENTION.days))
        today = self._clock.now().date()
        since = today - timedelta(days=days - 1)
        counts = await self._store.failed_login_counts(since)
        return fill_series(counts, since, today, Bucket.DAY)

    async def users(
        self,
        identity: Identity,
        *,
        query: str = "",
        status: UserStatus = UserStatus.ALL,
        sort: UserSort = UserSort.JOINED,
        descending: bool = True,
        cursor: str | None = None,
        limit: int = 25,
    ) -> Page[AdminUser]:
        _require_admin(identity)
        query = " ".join(query.split())[:MAX_QUERY]
        offset = _offset(cursor)
        limit = max(1, min(limit, MAX_PAGE))
        items, total = await self._store.list_users(
            query=query, status=status, sort=sort, descending=descending,
            offset=offset, limit=limit, now=self._clock.now(),
        )  # fmt: skip
        more = offset + len(items) < total
        return Page(tuple(items), str(offset + len(items)) if more else None, total)

    async def events(
        self,
        identity: Identity,
        *,
        kind: AuthEventKind | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Page[AuthEvent]:
        _require_admin(identity)
        before = _offset(cursor) or None
        limit = max(1, min(limit, MAX_PAGE))
        items = await self._store.list_events(kind=kind, before_id=before, limit=limit + 1)
        more = len(items) > limit
        items = items[:limit]
        return Page(tuple(items), str(items[-1].id) if more and items else None)

    async def set_disabled(
        self, identity: Identity, user_id: UserId, *, disabled: bool
    ) -> AdminUser:
        """Disable or re-enable an account. Disabling signs it out everywhere at once:
        the session lookup also joins away disabled users, so even a request already in
        flight with the old cookie resolves to no one."""
        _require_admin(identity)
        if user_id == identity.user_id:
            raise PermissionDenied("You can't disable your own account.")
        target = await self._store.get_user(user_id)
        if target is None:
            raise NotFound("That account no longer exists.")
        if self._owner is not None and canonical_email(target.email) == self._owner:
            raise PermissionDenied("The platform admin account can't be disabled.")
        changed = await self._store.set_disabled(user_id, disabled, actor_id=identity.user_id)
        if disabled:
            # Even on a retry (unchanged): cheap, and guarantees no session survives.
            await self._sessions.delete_for_user(user_id)
        if changed:
            log.info("admin.account_status", user_id=str(user_id), disabled=disabled)
        updated = await self._store.get_user(user_id)
        if updated is None:
            raise NotFound("That account no longer exists.")
        return updated


async def sweep_auth_events(events: AuthEventLog, clock: Clock) -> int:
    """Retention for the event log (worker housekeeping)."""
    deleted = await events.delete_before(clock.now() - AUTH_EVENT_RETENTION)
    if deleted:
        log.info("swept_auth_events", count=deleted)
    return deleted


def _require_admin(identity: Identity) -> None:
    if not (identity.is_platform_admin and identity.email_verified):
        raise NotFound(NOT_FOUND)


def _offset(cursor: str | None) -> int:
    if not cursor:
        return 0
    if not cursor.isdigit() or len(cursor) > 12:
        raise ValidationFailed("That page link is no longer valid. Reload the list.")
    return int(cursor)
