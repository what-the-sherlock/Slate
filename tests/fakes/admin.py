"""AdminStore fake over the in-memory account, session and event fakes (ADR-0042)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

from core.domain.admin import (
    ACTIVE_WINDOW,
    AdminUser,
    AuthEvent,
    AuthEventKind,
    Bucket,
    Overview,
    UserSort,
    UserStatus,
    bucket_start,
)
from core.domain.ids import UserId
from tests.fakes.auth import InMemoryAccounts, InMemoryAuthEvents, InMemorySessions

_FAILED = (AuthEventKind.LOGIN_FAILED, AuthEventKind.LOGIN_THROTTLED)
_NEVER = datetime.min.replace(tzinfo=UTC)


class InMemoryAdminStore:
    def __init__(
        self, accounts: InMemoryAccounts, sessions: InMemorySessions, events: InMemoryAuthEvents
    ) -> None:
        self._accounts = accounts
        self._sessions = sessions
        self._events = events

    def _last_active(self, uid: UserId) -> datetime | None:
        times = [
            row["last_seen_at"]
            for row in self._sessions.rows.values()
            if row["user_id"] == uid and isinstance(row["last_seen_at"], datetime)
        ]
        times += [
            r[3] for r in self._events.rows if r[1] == uid and r[0] is AuthEventKind.LOGIN_SUCCESS
        ]
        return max(times) if times else None

    def _user(self, uid: UserId) -> AdminUser:
        rec = self._accounts.users[uid]
        workspaces = (1 if uid in self._accounts.members else 0) + len(
            self._accounts.extra.get(uid, {})
        )
        return AdminUser(
            id=uid, email=rec.email, name=rec.name, joined_at=self._accounts.created_at[uid],
            email_verified=rec.email_verified, disabled=rec.disabled,
            last_active_at=self._last_active(uid), workspaces=workspaces,
        )  # fmt: skip

    async def overview(self, now: datetime) -> Overview:
        users = [self._user(u) for u in self._accounts.users]

        def since(days: int) -> datetime:
            return now - timedelta(days=days)

        def active(u: AdminUser, days: int) -> bool:
            seen = u.last_active_at
            return not u.disabled and seen is not None and seen >= since(days)

        return Overview(
            total=len(users),
            verified=sum(u.email_verified and not u.disabled for u in users),
            unverified=sum(not u.email_verified and not u.disabled for u in users),
            disabled=sum(u.disabled for u in users),
            new_today=sum(u.joined_at.date() == now.date() for u in users),
            new_7d=sum(u.joined_at >= since(7) for u in users),
            new_30d=sum(u.joined_at >= since(30) for u in users),
            new_prev_7d=sum(since(14) <= u.joined_at < since(7) for u in users),
            active_7d=sum(active(u, 7) for u in users),
            active_30d=sum(active(u, 30) for u in users),
            failed_logins_24h=sum(
                r[0] in _FAILED and r[3] >= now - timedelta(hours=24) for r in self._events.rows
            ),
        )

    async def signup_counts(self, since: date, bucket: Bucket) -> dict[date, int]:
        out: dict[date, int] = {}
        for at in self._accounts.created_at.values():
            if at.date() >= since:
                key = bucket_start(at.date(), bucket)
                out[key] = out.get(key, 0) + 1
        return out

    async def failed_login_counts(self, since: date) -> dict[date, int]:
        out: dict[date, int] = {}
        for kind, _, _, at in self._events.rows:
            if kind in _FAILED and at.date() >= since:
                out[at.date()] = out.get(at.date(), 0) + 1
        return out

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
        cutoff = now - ACTIVE_WINDOW
        q = query.lower()

        def keep(u: AdminUser) -> bool:
            if q and q not in u.email.lower() and q not in u.name.lower():
                return False
            recent = u.last_active_at is not None and u.last_active_at >= cutoff
            return {
                UserStatus.ALL: True,
                UserStatus.VERIFIED: not u.disabled and u.email_verified,
                UserStatus.UNVERIFIED: not u.disabled and not u.email_verified,
                UserStatus.DISABLED: u.disabled,
                UserStatus.ACTIVE: not u.disabled and recent,
                UserStatus.INACTIVE: not u.disabled and not recent,
            }[status]

        users = [u for u in (self._user(x) for x in self._accounts.users) if keep(u)]
        if sort is UserSort.EMAIL:
            users.sort(key=lambda u: u.email.lower(), reverse=descending)
        elif sort is UserSort.LAST_ACTIVE:  # NULLS LAST in both directions, like SQL
            seen = sorted(
                (u for u in users if u.last_active_at),
                key=lambda u: u.last_active_at or _NEVER,
                reverse=descending,
            )
            users = seen + [u for u in users if not u.last_active_at]
        else:
            users.sort(key=lambda u: u.joined_at, reverse=descending)
        return users[offset : offset + limit], len(users)

    async def get_user(self, user_id: UserId) -> AdminUser | None:
        return self._user(user_id) if user_id in self._accounts.users else None

    async def list_events(
        self, *, kind: AuthEventKind | None, before_id: int | None, limit: int
    ) -> list[AuthEvent]:
        rows = [
            AuthEvent(
                id=i + 1, kind=k, at=at, user_id=uid,
                user_email=self._accounts.users[uid].email
                if uid and uid in self._accounts.users else None,
            )
            for i, (k, uid, _, at) in enumerate(self._events.rows)
        ]  # fmt: skip
        rows = [e for e in rows if (kind is None or e.kind is kind)]
        rows = [e for e in rows if before_id is None or e.id < before_id]
        return sorted(rows, key=lambda e: e.id, reverse=True)[:limit]

    async def set_disabled(self, user_id: UserId, disabled: bool, *, actor_id: UserId) -> bool:
        rec = self._accounts.users.get(user_id)
        if rec is None or rec.disabled == disabled:
            return False
        self._accounts.users[user_id] = replace(rec, disabled=disabled)
        kind = AuthEventKind.ACCOUNT_DISABLED if disabled else AuthEventKind.ACCOUNT_ENABLED
        await self._events.record(kind, user_id, actor_id=actor_id)
        return True
