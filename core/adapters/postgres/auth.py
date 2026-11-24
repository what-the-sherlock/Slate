"""Account, session and login-throttle stores over Postgres."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import RowMapping, text

from core.adapters.postgres.uow import UnitOfWork
from core.domain.auth import Role
from core.domain.errors import Conflict
from core.domain.ids import SessionId, UserId, WorkspaceId
from core.ports.auth import SessionRecord, ThrottleLimits, UserRecord

# Any constant works; it just has to be the same for every setup attempt.
_SETUP_LOCK_KEY = 0x51A7E_0001
GUEST_EMAIL = "guest@slate.invalid"  # .invalid is reserved (RFC 2606): never a real inbox


def _user(row: RowMapping) -> UserRecord:
    return UserRecord(
        id=UserId(row["id"]),
        email=row["email"],
        name=row["name"],
        password_hash=row["password_hash"],
        is_guest=row["is_guest"],
        disabled=row["disabled_at"] is not None,
    )


class PostgresAccountStore:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def is_set_up(self) -> bool:
        async with self._uow() as s:
            q = "SELECT EXISTS (SELECT 1 FROM memberships WHERE role = 'owner')"
            return bool((await s.execute(text(q))).scalar_one())

    async def create_workspace_with_owner(
        self, *, workspace_name: str, slug: str, email: str, name: str, password_hash: str
    ) -> tuple[WorkspaceId, UserId]:
        async with self._uow() as s:
            # Serialise concurrent setups: the second waits here, then sees the owner.
            await s.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _SETUP_LOCK_KEY})
            exists = "SELECT EXISTS (SELECT 1 FROM memberships WHERE role = 'owner')"
            if (await s.execute(text(exists))).scalar_one():
                raise Conflict("This workspace is already set up. Sign in instead.")
            ws = (
                await s.execute(
                    text("INSERT INTO workspaces (name, slug) VALUES (:n, :s) RETURNING id"),
                    {"n": workspace_name, "s": slug},
                )
            ).scalar_one()
            owner = (
                await s.execute(
                    text(
                        "INSERT INTO users (email, name, password_hash)"
                        " VALUES (:e, :n, :h) RETURNING id"
                    ),
                    {"e": email, "n": name, "h": password_hash},
                )
            ).scalar_one()
            guest = (
                await s.execute(
                    text(
                        "INSERT INTO users (email, name, is_guest) VALUES (:e, 'Guest', true)"
                        " ON CONFLICT (email) DO UPDATE SET name = 'Guest' RETURNING id"
                    ),
                    {"e": GUEST_EMAIL},
                )
            ).scalar_one()
            await s.execute(
                text(
                    "INSERT INTO memberships (workspace_id, user_id, role) VALUES"
                    " (:ws, :owner, 'owner'), (:ws, :guest, 'viewer')"
                ),
                {"ws": ws, "owner": owner, "guest": guest},
            )
            return WorkspaceId(ws), UserId(owner)

    async def find_user_by_email(self, email: str) -> UserRecord | None:
        async with self._uow() as s:
            row = (
                (
                    await s.execute(
                        text(
                            "SELECT id, email, name, password_hash, is_guest, disabled_at"
                            " FROM users WHERE email = :e"
                        ),
                        {"e": email},
                    )
                )
                .mappings()
                .first()
            )
        return None if row is None else _user(row)

    async def find_guest(self) -> tuple[UserRecord, WorkspaceId] | None:
        async with self._uow() as s:
            row = (
                (
                    await s.execute(
                        text(
                            "SELECT u.id, u.email, u.name, u.password_hash, u.is_guest,"
                            " u.disabled_at, m.workspace_id FROM users u"
                            " JOIN memberships m ON m.user_id = u.id"
                            " WHERE u.is_guest AND u.disabled_at IS NULL LIMIT 1"
                        )
                    )
                )
                .mappings()
                .first()
            )
        return None if row is None else (_user(row), WorkspaceId(row["workspace_id"]))

    async def workspace_for_user(self, user_id: UserId) -> tuple[WorkspaceId, Role] | None:
        async with self._uow() as s:
            row = (
                await s.execute(
                    text("SELECT workspace_id, role FROM memberships WHERE user_id = :u LIMIT 1"),
                    {"u": user_id},
                )
            ).first()
        return None if row is None else (WorkspaceId(row[0]), Role(row[1]))


class PostgresSessionStore:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def create(
        self,
        *,
        user_id: UserId,
        token_hash: bytes,
        csrf_secret: str,
        ttl: timedelta,
        ip: str | None,
        user_agent: str | None,
    ) -> SessionId:
        async with self._uow() as s:
            sid = (
                await s.execute(
                    text(
                        "INSERT INTO sessions (user_id, token_hash, csrf_secret, expires_at,"
                        " ip, user_agent) VALUES (:u, :t, :c,"
                        " now() + make_interval(secs => :ttl), CAST(:ip AS inet), :ua)"
                        " RETURNING id"
                    ),
                    {
                        "u": user_id,
                        "t": token_hash,
                        "c": csrf_secret,
                        "ttl": ttl.total_seconds(),
                        "ip": ip,
                        "ua": user_agent,
                    },
                )
            ).scalar_one()
            # Opportunistic cleanup keeps the table small without a cron job.
            await s.execute(
                text("DELETE FROM sessions WHERE expires_at < now() - interval '1 day'")
            )
            return SessionId(sid)

    async def get(self, token_hash: bytes) -> SessionRecord | None:
        async with self._uow() as s:
            row = (
                (
                    await s.execute(
                        text(
                            "SELECT s.id, s.user_id, s.csrf_secret, s.expires_at, s.last_seen_at,"
                            " u.email, u.name, u.is_guest, m.workspace_id, m.role,"
                            " coalesce(array_agg(gm.group_id::text)"
                            # g.id, not gm.group_id: only groups in *this* workspace count
                            "   FILTER (WHERE g.id IS NOT NULL), '{}') AS group_ids"
                            " FROM sessions s"
                            " JOIN users u ON u.id = s.user_id AND u.disabled_at IS NULL"
                            " JOIN memberships m ON m.user_id = u.id"
                            " LEFT JOIN group_members gm ON gm.user_id = u.id"
                            " LEFT JOIN groups g ON g.id = gm.group_id"
                            "   AND g.workspace_id = m.workspace_id"
                            " WHERE s.token_hash = :t AND s.expires_at > now()"
                            " GROUP BY s.id, u.id, m.workspace_id, m.role"
                        ),
                        {"t": token_hash},
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        groups: Any = row["group_ids"]  # reason: Postgres text[] decodes to list[str]
        return SessionRecord(
            id=SessionId(row["id"]),
            user_id=UserId(row["user_id"]),
            workspace_id=WorkspaceId(row["workspace_id"]),
            role=Role(row["role"]),
            email=row["email"],
            name=row["name"],
            is_guest=row["is_guest"],
            csrf_secret=row["csrf_secret"],
            expires_at=row["expires_at"],
            last_seen_at=row["last_seen_at"],
            group_ids=tuple(str(g) for g in groups),
        )

    async def touch(self, session_id: SessionId, ttl: timedelta) -> None:
        async with self._uow() as s:
            await s.execute(
                text(
                    "UPDATE sessions SET last_seen_at = now(),"
                    " expires_at = now() + make_interval(secs => :ttl) WHERE id = :id"
                ),
                {"id": session_id, "ttl": ttl.total_seconds()},
            )

    async def delete(self, session_id: SessionId) -> None:
        async with self._uow() as s:
            await s.execute(text("DELETE FROM sessions WHERE id = :id"), {"id": session_id})

    async def delete_for_user(self, user_id: UserId) -> None:
        async with self._uow() as s:
            await s.execute(text("DELETE FROM sessions WHERE user_id = :u"), {"u": user_id})

    async def count_active_for_user(self, user_id: UserId) -> int:
        async with self._uow() as s:
            q = "SELECT count(*) FROM sessions WHERE user_id = :u AND expires_at > now()"
            return int((await s.execute(text(q), {"u": user_id})).scalar_one())


class PostgresLoginThrottle:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def begin_attempt(self, *, email: str, ip: str, limits: ThrottleLimits) -> int | None:
        async with self._uow() as s:
            # Serialise attempts for the same email and the same IP (always in this order,
            # so two transactions can't deadlock), then check and record atomically.
            await s.execute(
                text(
                    "SELECT pg_advisory_xact_lock(hashtext('login-email:' || :e)),"
                    " pg_advisory_xact_lock(hashtext('login-ip:' || :ip))"
                ),
                {"e": email, "ip": ip},
            )
            row = (
                await s.execute(
                    text(
                        "SELECT"
                        " count(*) FILTER (WHERE email = :e AND ip = CAST(:ip AS inet)),"
                        " count(*) FILTER (WHERE ip = CAST(:ip AS inet)),"
                        " count(*) FILTER (WHERE email = :e)"
                        " FROM login_attempts WHERE NOT success"
                        " AND created_at > now() - make_interval(secs => :w)"
                    ),
                    {"e": email, "ip": ip, "w": limits.window.total_seconds()},
                )
            ).one()
            if (
                row[0] >= limits.per_email_ip
                or row[1] >= limits.per_ip
                or row[2] >= limits.per_email
            ):
                return None
            attempt = (
                await s.execute(
                    text(
                        "INSERT INTO login_attempts (email, ip, success)"
                        " VALUES (:e, CAST(:ip AS inet), false) RETURNING id"
                    ),
                    {"e": email, "ip": ip},
                )
            ).scalar_one()
            await s.execute(
                text("DELETE FROM login_attempts WHERE created_at < now() - interval '1 day'")
            )
            return int(attempt)

    async def mark_success(self, attempt_id: int) -> None:
        async with self._uow() as s:
            await s.execute(
                text("UPDATE login_attempts SET success = true WHERE id = :id"), {"id": attempt_id}
            )
