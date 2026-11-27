"""Auth event log and the admin console's platform-wide reads, over Postgres (ADR-0042).

These queries are platform-wide on purpose (they count accounts, not content), so they are
not workspace-scoped and never touch documents, chunks, chats or sources. Only
AdminService calls them, after its admin check.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import RowMapping, text

from core.adapters.postgres.uow import UnitOfWork
from core.adapters.postgres.workspaces import REMOVE_MEMBER_SQL
from core.domain.admin import (
    ACTIVE_WINDOW,
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


class PostgresAuthEventLog:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def record(
        self, kind: AuthEventKind, user_id: UserId | None, *, actor_id: UserId | None = None
    ) -> None:
        async with self._uow() as s:
            await s.execute(
                text("INSERT INTO auth_events (kind, user_id, actor_id) VALUES (:k, :u, :a)"),
                {"k": kind.value, "u": user_id, "a": actor_id},
            )

    async def delete_before(self, cutoff: datetime) -> int:
        async with self._uow() as s:
            rows = (
                await s.execute(
                    text("DELETE FROM auth_events WHERE created_at < :c RETURNING id"),
                    {"c": cutoff},
                )
            ).all()
        return len(rows)


# Last activity is users.last_active_at, written on sign-in and every session touch
# (migration 0015), so it survives session and event cleanup.
_OVERVIEW = (
    "SELECT"
    " count(*) AS total,"
    " count(*) FILTER (WHERE u.disabled_at IS NULL AND u.email_verified_at IS NOT NULL)"
    "   AS verified,"
    " count(*) FILTER (WHERE u.disabled_at IS NULL AND u.email_verified_at IS NULL)"
    "   AS unverified,"
    " count(*) FILTER (WHERE u.disabled_at IS NOT NULL) AS disabled,"
    " count(*) FILTER (WHERE u.created_at >= date_trunc('day', CAST(:now AS timestamptz),"
    "   'UTC')) AS new_today,"
    " count(*) FILTER (WHERE u.created_at >= CAST(:now AS timestamptz) - interval '7 days')"
    "   AS new_7d,"
    " count(*) FILTER (WHERE u.created_at >= CAST(:now AS timestamptz) - interval '30 days')"
    "   AS new_30d,"
    " count(*) FILTER (WHERE u.created_at >= CAST(:now AS timestamptz) - interval '14 days'"
    "   AND u.created_at < CAST(:now AS timestamptz) - interval '7 days') AS new_prev_7d,"
    " count(*) FILTER (WHERE u.disabled_at IS NULL"
    "   AND u.last_active_at >= CAST(:now AS timestamptz) - interval '7 days') AS active_7d,"
    " count(*) FILTER (WHERE u.disabled_at IS NULL"
    "   AND u.last_active_at >= CAST(:now AS timestamptz) - interval '30 days') AS active_30d,"
    " (SELECT count(*) FROM auth_events WHERE kind IN ('login_failed', 'login_throttled')"
    "   AND created_at >= CAST(:now AS timestamptz) - interval '24 hours') AS failed_24h"
    " FROM users u"
)

_USER_COLUMNS = (
    "u.id, u.email, u.name, u.created_at, u.email_verified_at, u.disabled_at,"
    " u.last_active_at,"
    " (SELECT count(*) FROM memberships m WHERE m.user_id = u.id) AS workspaces"
)

# Fixed fragments only: user input never reaches these strings.
_STATUS_WHERE = {
    UserStatus.ALL: "true",
    UserStatus.VERIFIED: "u.disabled_at IS NULL AND u.email_verified_at IS NOT NULL",
    UserStatus.UNVERIFIED: "u.disabled_at IS NULL AND u.email_verified_at IS NULL",
    UserStatus.DISABLED: "u.disabled_at IS NOT NULL",
    UserStatus.ACTIVE: "u.disabled_at IS NULL AND u.last_active_at >= :active_since",
    UserStatus.INACTIVE: (
        "u.disabled_at IS NULL AND (u.last_active_at IS NULL OR u.last_active_at < :active_since)"
    ),
}
_SORT = {
    UserSort.JOINED: "u.created_at",
    UserSort.LAST_ACTIVE: "u.last_active_at",
    UserSort.EMAIL: "lower(u.email)",
}


def escape_like(q: str) -> str:
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _admin_user(row: RowMapping) -> AdminUser:
    return AdminUser(
        id=UserId(row["id"]),
        email=row["email"],
        name=row["name"],
        joined_at=row["created_at"],
        email_verified=row["email_verified_at"] is not None,
        disabled=row["disabled_at"] is not None,
        last_active_at=row["last_active_at"],
        workspaces=int(row["workspaces"]),
    )


class PostgresAdminStore:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def overview(self, now: datetime) -> Overview:
        async with self._uow() as s:
            r = (await s.execute(text(_OVERVIEW), {"now": now})).mappings().one()
        return Overview(
            total=int(r["total"]),
            verified=int(r["verified"]),
            unverified=int(r["unverified"]),
            disabled=int(r["disabled"]),
            new_today=int(r["new_today"]),
            new_7d=int(r["new_7d"]),
            new_30d=int(r["new_30d"]),
            new_prev_7d=int(r["new_prev_7d"]),
            active_7d=int(r["active_7d"]),
            active_30d=int(r["active_30d"]),
            failed_logins_24h=int(r["failed_24h"]),
        )

    async def signup_counts(self, since: date, bucket: Bucket) -> dict[date, int]:
        return await self._counts(
            "SELECT CAST(date_trunc(:b, created_at AT TIME ZONE 'UTC') AS date) AS d,"
            " count(*) AS n FROM users"
            " WHERE created_at >= CAST(:since AS timestamp) AT TIME ZONE 'UTC'"
            " GROUP BY 1",
            {"b": bucket.value, "since": since},
        )

    async def failed_login_counts(self, since: date) -> dict[date, int]:
        return await self._counts(
            "SELECT CAST(created_at AT TIME ZONE 'UTC' AS date) AS d, count(*) AS n"
            " FROM auth_events WHERE kind IN ('login_failed', 'login_throttled')"
            " AND created_at >= CAST(:since AS timestamp) AT TIME ZONE 'UTC' GROUP BY 1",
            {"since": since},
        )

    async def _counts(self, sql: str, params: dict[str, Any]) -> dict[date, int]:
        async with self._uow() as s:
            rows = (await s.execute(text(sql), params)).all()
        return {row[0]: int(row[1]) for row in rows}

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
        where = _STATUS_WHERE[status]
        params: dict[str, Any] = {"active_since": now - ACTIVE_WINDOW, "lim": limit, "off": offset}
        if query:
            where += " AND (u.email ILIKE :q ESCAPE '\\' OR u.name ILIKE :q ESCAPE '\\')"
            params["q"] = f"%{escape_like(query)}%"
        direction = "DESC" if descending else "ASC"
        # NULLS LAST either way: "never active" sorts after everyone, which is what you
        # want whether you're looking for the newest or the stalest accounts.
        order = f"{_SORT[sort]} {direction} NULLS LAST, u.id {direction}"
        base = f"SELECT {{cols}} FROM users u WHERE {where}"  # noqa: S608 (fixed fragments only)
        async with self._uow() as s:
            rows = (
                (
                    await s.execute(
                        text(
                            base.format(cols=_USER_COLUMNS)
                            + f" ORDER BY {order} LIMIT :lim OFFSET :off"
                        ),
                        params,
                    )
                )
                .mappings()
                .all()
            )
            total = (await s.execute(text(base.format(cols="count(*)")), params)).scalar_one()
        return [_admin_user(r) for r in rows], int(total)

    async def get_user(self, user_id: UserId) -> AdminUser | None:
        sql = f"SELECT {_USER_COLUMNS} FROM users u WHERE u.id = :id"  # noqa: S608
        async with self._uow() as s:
            row = (await s.execute(text(sql), {"id": user_id})).mappings().first()
        return None if row is None else _admin_user(row)

    async def list_events(
        self, *, kind: AuthEventKind | None, before_id: int | None, limit: int
    ) -> list[AuthEvent]:
        async with self._uow() as s:
            rows = (
                (
                    await s.execute(
                        text(
                            "SELECT e.id, e.kind, e.created_at, e.user_id, u.email"
                            " FROM auth_events e LEFT JOIN users u ON u.id = e.user_id"
                            " WHERE (CAST(:k AS text) IS NULL OR e.kind = :k)"
                            " AND (CAST(:b AS bigint) IS NULL OR e.id < :b)"
                            " ORDER BY e.id DESC LIMIT :lim"
                        ),
                        {"k": kind.value if kind else None, "b": before_id, "lim": limit},
                    )
                )
                .mappings()
                .all()
            )
        return [
            AuthEvent(
                id=int(r["id"]),
                kind=AuthEventKind(r["kind"]),
                at=r["created_at"],
                user_id=UserId(r["user_id"]) if r["user_id"] else None,
                user_email=r["email"],
            )
            for r in rows
        ]

    async def delete_user(self, user_id: UserId, *, actor_id: UserId) -> DeleteOutcome:
        async with self._uow() as s:
            # Lock the account first: concurrent invites, sign-ins and a second delete wait.
            found = (
                await s.execute(
                    text("SELECT id FROM users WHERE id = :u FOR UPDATE"), {"u": user_id}
                )
            ).first()
            if found is None:
                return DeleteOutcome.NOT_FOUND
            # Lock their workspaces before counting members: accepting an invite inserts a
            # membership, whose foreign key needs a share lock on the workspace row, so a
            # concurrent join waits until we're done instead of slipping past the check.
            memberships = (
                await s.execute(
                    text(
                        "SELECT m.workspace_id, m.role FROM memberships m"
                        " JOIN workspaces w ON w.id = m.workspace_id"
                        " WHERE m.user_id = :u FOR UPDATE OF m, w"
                    ),
                    {"u": user_id},
                )
            ).all()
            counts = dict(
                (
                    await s.execute(
                        text(
                            "SELECT workspace_id, count(*) FROM memberships"
                            " WHERE workspace_id = ANY(:ws) GROUP BY workspace_id"
                        ),
                        {"ws": [ws for ws, _ in memberships]},
                    )
                ).all()
            )
            memberships = [(ws, role, int(counts.get(ws, 1))) for ws, role in memberships]
            if any(role == "owner" and members > 1 for _, role, members in memberships):
                return DeleteOutcome.OWNS_SHARED_WORKSPACE  # nothing changed
            for ws, role, _ in memberships:
                if role == "owner":  # a workspace only they belong to: content cascades
                    await s.execute(text("DELETE FROM workspaces WHERE id = :ws"), {"ws": ws})
                else:  # someone else's workspace: the same cleanup as "Remove member"
                    for statement in REMOVE_MEMBER_SQL.split(";"):
                        if statement.strip():
                            await s.execute(text(statement), {"ws": ws, "u": user_id})
            await s.execute(
                text("INSERT INTO auth_events (kind, user_id, actor_id) VALUES (:k, NULL, :a)"),
                {"k": AuthEventKind.ACCOUNT_DELETED.value, "a": actor_id},
            )
            await s.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})
        return DeleteOutcome.DELETED

    async def set_disabled(self, user_id: UserId, disabled: bool, *, actor_id: UserId) -> bool:
        # One statement: the change and its event commit together or not at all, and an
        # unchanged status updates no row, so a retry never writes a second event.
        sql = (
            "WITH u AS ("
            "  UPDATE users SET disabled_at = CASE WHEN :d THEN now() ELSE NULL END"
            "  WHERE id = :id AND (disabled_at IS NOT NULL) <> :d RETURNING id"
            ") INSERT INTO auth_events (kind, user_id, actor_id)"
            " SELECT :k, id, :a FROM u RETURNING id"
        )
        kind = AuthEventKind.ACCOUNT_DISABLED if disabled else AuthEventKind.ACCOUNT_ENABLED
        params = {"d": disabled, "id": user_id, "k": kind.value, "a": actor_id}
        async with self._uow() as s:
            row = (await s.execute(text(sql), params)).first()
        return row is not None
