"""Account, session, one-time-code and login-throttle stores over Postgres."""

from __future__ import annotations

import hmac
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import RowMapping, text

from core.adapters.postgres.uow import UnitOfWork
from core.domain.auth import CodeCheck, CodePurpose, Role
from core.domain.errors import Conflict
from core.domain.ids import SessionId, UserId, WorkspaceId
from core.ports.auth import SessionRecord, ThrottleLimits, UserRecord


def _user(row: RowMapping) -> UserRecord:
    return UserRecord(
        id=UserId(row["id"]),
        email=row["email"],
        name=row["name"],
        password_hash=row["password_hash"],
        email_verified=row["email_verified_at"] is not None,
        disabled=row["disabled_at"] is not None,
    )


# One statement, one round trip, one transaction: the user, their personal workspace and
# the owner membership (ADR-0021). ON CONFLICT DO NOTHING (no target) covers both unique
# indexes, email and email_canonical. A sign-up that loses a race inserts no user, so the
# later CTEs insert nothing and no row comes back. The index decides, not a prior SELECT.
_CREATE_USER_WITH_WORKSPACE = (
    "WITH u AS ("
    "  INSERT INTO users (email, email_canonical, name, password_hash)"
    "  VALUES (:e, :ec, :n, :h) ON CONFLICT DO NOTHING RETURNING id"
    "), w AS ("
    "  INSERT INTO workspaces (name, slug) SELECT :wn, :slug FROM u RETURNING id"
    "), m AS ("
    "  INSERT INTO memberships (workspace_id, user_id, role)"
    "  SELECT w.id, u.id, 'owner' FROM u, w RETURNING workspace_id, user_id"
    ") SELECT workspace_id, user_id FROM m"
)
# Never-verified accounts past the cutoff, plus the personal workspaces only they belong to
# (their content cascades). A workspace with any other member is left alone.
_DELETE_UNVERIFIED = (
    "WITH doomed AS ("
    "  SELECT id FROM users WHERE email_verified_at IS NULL AND created_at < :cutoff"
    "), ws AS ("
    "  DELETE FROM workspaces w USING memberships m"
    "  WHERE m.workspace_id = w.id AND m.user_id IN (SELECT id FROM doomed)"
    "    AND NOT EXISTS (SELECT 1 FROM memberships o"
    "                    WHERE o.workspace_id = w.id AND o.user_id <> m.user_id)"
    "  RETURNING w.id"
    ") DELETE FROM users WHERE id IN (SELECT id FROM doomed) RETURNING id"
)

# A session with its user, membership and groups. The joins are the access rules: a
# disabled user or a non-member resolves to no row (and memberships_one_per_user means a
# user resolves to exactly one workspace). {source} and {where} are filled below from
# fixed fragments only, at import time.
_RESOLVE_SESSION = (
    "SELECT s.id, s.user_id, s.csrf_secret, s.expires_at, s.last_seen_at,"
    " u.email, u.name, u.email_verified_at IS NOT NULL AS email_verified,"
    " m.workspace_id, m.role,"
    " coalesce(array_agg(gm.group_id::text)"
    # g.id, not gm.group_id: only groups in *this* workspace count
    "   FILTER (WHERE g.id IS NOT NULL), '{{}}') AS group_ids"
    " FROM {source} s"
    " JOIN users u ON u.id = s.user_id AND u.disabled_at IS NULL"
    " JOIN memberships m ON m.user_id = u.id"
    " LEFT JOIN group_members gm ON gm.user_id = u.id"
    " LEFT JOIN groups g ON g.id = gm.group_id"
    "   AND g.workspace_id = m.workspace_id"
    " WHERE {where}"
    " GROUP BY s.id, s.user_id, s.csrf_secret, s.expires_at, s.last_seen_at,"
    " u.id, m.workspace_id, m.role"
)
_GET_SESSION = _RESOLVE_SESSION.format(
    source="sessions", where="s.token_hash = :t AND s.expires_at > now()"
)
# One statement: data-modifying CTEs insert the session and clean up old ones (keeps the
# table small without a cron job), and the outer query resolves the new row exactly as
# _GET_SESSION does. Saves the read-back transaction after every login.
_CREATE_SESSION = (
    "WITH s_new AS ("  # noqa: S608 (built from the fixed fragments above)
    "  INSERT INTO sessions (user_id, token_hash, csrf_secret, expires_at, ip, user_agent)"
    "  VALUES (:u, :t, :c, now() + make_interval(secs => :ttl), CAST(:ip AS inet), :ua)"
    "  RETURNING *"
    "), gc AS ("
    "  DELETE FROM sessions WHERE expires_at < now() - interval '1 day'"
    ") " + _RESOLVE_SESSION.format(source="s_new", where="true")
)


def _session(row: RowMapping) -> SessionRecord:
    groups: Any = row["group_ids"]  # reason: Postgres text[] decodes to list[str]
    return SessionRecord(
        id=SessionId(row["id"]),
        user_id=UserId(row["user_id"]),
        workspace_id=WorkspaceId(row["workspace_id"]),
        role=Role(row["role"]),
        email=row["email"],
        name=row["name"],
        email_verified=row["email_verified"],
        csrf_secret=row["csrf_secret"],
        expires_at=row["expires_at"],
        last_seen_at=row["last_seen_at"],
        group_ids=tuple(str(g) for g in groups),
    )


class PostgresAccountStore:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def create_user_with_workspace(
        self,
        *,
        email: str,
        email_canonical: str,
        name: str,
        password_hash: str,
        workspace_name: str,
        slug: str,
    ) -> tuple[WorkspaceId, UserId]:
        params = {
            "e": email,
            "ec": email_canonical,
            "n": name,
            "h": password_hash,
            "wn": workspace_name,
            "slug": slug,
        }
        async with self._uow() as s:
            row = (await s.execute(text(_CREATE_USER_WITH_WORKSPACE), params)).first()
        if row is None:
            raise Conflict("An account with this email already exists.")
        return WorkspaceId(row[0]), UserId(row[1])

    async def find_user(self, email_canonical: str) -> UserRecord | None:
        async with self._uow() as s:
            row = (
                (
                    await s.execute(
                        text(
                            "SELECT id, email, name, password_hash, email_verified_at,"
                            " disabled_at FROM users WHERE email_canonical = :e"
                        ),
                        {"e": email_canonical},
                    )
                )
                .mappings()
                .first()
            )
        return None if row is None else _user(row)

    async def mark_email_verified(self, user_id: UserId) -> None:
        async with self._uow() as s:
            await s.execute(
                text(
                    "UPDATE users SET email_verified_at = now()"
                    " WHERE id = :u AND email_verified_at IS NULL"
                ),
                {"u": user_id},
            )

    async def set_password(self, user_id: UserId, password_hash: str) -> None:
        async with self._uow() as s:
            await s.execute(
                text("UPDATE users SET password_hash = :h WHERE id = :u"),
                {"u": user_id, "h": password_hash},
            )

    async def delete_unverified_before(self, cutoff: datetime) -> int:
        async with self._uow() as s:
            rows = (await s.execute(text(_DELETE_UNVERIFIED), {"cutoff": cutoff})).all()
        return len(rows)


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
    ) -> SessionRecord | None:
        params = {
            "u": user_id,
            "t": token_hash,
            "c": csrf_secret,
            "ttl": ttl.total_seconds(),
            "ip": ip,
            "ua": user_agent,
        }
        async with self._uow() as s:
            row = (await s.execute(text(_CREATE_SESSION), params)).mappings().first()
        return None if row is None else _session(row)

    async def get(self, token_hash: bytes) -> SessionRecord | None:
        async with self._uow() as s:
            row = (await s.execute(text(_GET_SESSION), {"t": token_hash})).mappings().first()
        return None if row is None else _session(row)

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


class PostgresCodeStore:
    """One live code per (user, purpose); a row lock makes check-and-update atomic."""

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def replace(
        self, user_id: UserId, purpose: CodePurpose, code_hash: bytes, ttl: timedelta
    ) -> None:
        async with self._uow() as s:
            await s.execute(
                text(
                    "INSERT INTO email_codes (user_id, purpose, code_hash, expires_at)"
                    " VALUES (:u, :p, :h, now() + make_interval(secs => :ttl))"
                    " ON CONFLICT (user_id, purpose) DO UPDATE SET"
                    "  code_hash = excluded.code_hash, expires_at = excluded.expires_at,"
                    "  attempts = 0, consumed_at = NULL, created_at = now()"
                ),
                {"u": user_id, "p": purpose.value, "h": code_hash, "ttl": ttl.total_seconds()},
            )

    async def check(
        self, user_id: UserId, purpose: CodePurpose, code_hash: bytes, *, max_attempts: int
    ) -> CodeCheck:
        key = {"u": user_id, "p": purpose.value}
        async with self._uow() as s:
            # FOR UPDATE: parallel guesses queue on the row, so they can't all see 4 attempts.
            row = (
                await s.execute(
                    text(
                        "SELECT code_hash, attempts,"
                        " consumed_at IS NULL AND expires_at > now() AS live"
                        " FROM email_codes WHERE user_id = :u AND purpose = :p FOR UPDATE"
                    ),
                    key,
                )
            ).first()
            if row is None or not row[2]:
                return CodeCheck.EXPIRED
            if row[1] >= max_attempts:
                return CodeCheck.LOCKED
            if hmac.compare_digest(bytes(row[0]), code_hash):
                done = "UPDATE email_codes SET consumed_at = now()"
                outcome = CodeCheck.OK
            else:
                done = "UPDATE email_codes SET attempts = attempts + 1"
                outcome = CodeCheck.WRONG
            await s.execute(text(done + " WHERE user_id = :u AND purpose = :p"), key)
            return outcome


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
