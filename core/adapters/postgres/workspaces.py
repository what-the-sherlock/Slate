"""WorkspaceStore over Postgres (ADR-0030)."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text

from core.adapters.postgres.uow import UnitOfWork
from core.domain.auth import Role
from core.domain.errors import Conflict
from core.domain.ids import SourceId, UserId, WorkspaceId
from core.domain.principals import user_principal, workspace_principal
from core.ports.workspaces import InviteRecord, InviteView, MemberView, WorkspaceMembership

REMOVE_MEMBER_SQL = (
    # Everything they connected here (documents, chunks and runs cascade; the 0007 trigger
    # deletes stored credentials), their uploads in the shared upload sources, their chats.
    "DELETE FROM sources WHERE workspace_id = :ws AND created_by = :u AND connector <> 'upload';"
    "DELETE FROM documents d USING sources s"
    " WHERE d.source_id = s.id AND s.workspace_id = :ws AND s.connector = 'upload'"
    "   AND d.metadata ->> 'uploader' = CAST(:u AS text);"
    "DELETE FROM chat_sessions WHERE workspace_id = :ws AND user_id = :u;"
    "DELETE FROM jobs WHERE workspace_id = :ws AND status IN ('queued', 'failed')"
    " AND payload ->> 'uploader' = CAST(:u AS text);"
    "UPDATE sessions SET workspace_id = NULL WHERE user_id = :u AND workspace_id = :ws;"
)


class PostgresWorkspaceStore:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def memberships(self, user: UserId) -> list[WorkspaceMembership]:
        async with self._uow() as s:
            rows = (
                await s.execute(
                    text(
                        "SELECT m.workspace_id, w.name, m.role,"
                        " (SELECT count(*) FROM memberships o"
                        "  WHERE o.workspace_id = m.workspace_id)"
                        " FROM memberships m JOIN workspaces w ON w.id = m.workspace_id"
                        " WHERE m.user_id = :u ORDER BY (m.role = 'owner') DESC, m.created_at"
                    ),
                    {"u": user},
                )
            ).all()
        return [
            WorkspaceMembership(WorkspaceId(r[0]), str(r[1]), Role(r[2]), int(r[3])) for r in rows
        ]

    async def role_of(self, ws: WorkspaceId, user: UserId) -> Role | None:
        async with self._uow() as s:
            role = (
                await s.execute(
                    text("SELECT role FROM memberships WHERE workspace_id = :ws AND user_id = :u"),
                    {"ws": ws, "u": user},
                )
            ).scalar_one_or_none()
        return Role(role) if role is not None else None

    async def members(self, ws: WorkspaceId) -> list[MemberView]:
        async with self._uow() as s:
            rows = (
                await s.execute(
                    text(
                        "SELECT u.id, u.email, u.name, m.role, m.created_at FROM memberships m"
                        " JOIN users u ON u.id = m.user_id WHERE m.workspace_id = :ws"
                        " ORDER BY (m.role = 'owner') DESC, m.created_at"
                    ),
                    {"ws": ws},
                )
            ).all()
        return [MemberView(UserId(r[0]), str(r[1]), str(r[2]), Role(r[3]), r[4]) for r in rows]

    async def member_count(self, ws: WorkspaceId) -> int:
        async with self._uow() as s:
            return int(
                (
                    await s.execute(
                        text("SELECT count(*) FROM memberships WHERE workspace_id = :ws"),
                        {"ws": ws},
                    )
                ).scalar_one()
            )

    async def workspace_name(self, ws: WorkspaceId) -> str | None:
        async with self._uow() as s:
            name = (
                await s.execute(text("SELECT name FROM workspaces WHERE id = :ws"), {"ws": ws})
            ).scalar_one_or_none()
        return str(name) if name is not None else None

    async def create_invite(
        self,
        ws: WorkspaceId,
        *,
        email_canonical: str,
        role: Role,
        token_hash: bytes,
        invited_by: UserId,
        expires_at: datetime,
    ) -> UUID:
        async with self._uow() as s:
            await s.execute(
                text(
                    "UPDATE invitations SET revoked_at = now() WHERE workspace_id = :ws"
                    " AND email_canonical = CAST(:e AS citext)"
                    " AND accepted_at IS NULL AND revoked_at IS NULL"
                ),
                {"ws": ws, "e": email_canonical},
            )
            iid = (
                await s.execute(
                    text(
                        # A concurrent invite to the same address wins the partial unique
                        # index; this one then inserts nothing and the service says so.
                        "INSERT INTO invitations (workspace_id, email_canonical, role, token_hash,"
                        " invited_by, expires_at) VALUES (:ws, :e, :r, :t, :by, :exp)"
                        " ON CONFLICT DO NOTHING RETURNING id"
                    ),
                    {
                        "ws": ws,
                        "e": email_canonical,
                        "r": role.value,
                        "t": token_hash,
                        "by": invited_by,
                        "exp": expires_at,
                    },
                )
            ).scalar_one_or_none()
        if iid is None:
            raise Conflict("An invitation to that address was just sent. Try again in a moment.")
        return UUID(str(iid))

    async def invites(self, ws: WorkspaceId) -> list[InviteView]:
        async with self._uow() as s:
            rows = (
                await s.execute(
                    text(
                        "SELECT id, email_canonical, role, created_at, expires_at FROM invitations"
                        " WHERE workspace_id = :ws AND accepted_at IS NULL AND revoked_at IS NULL"
                        " AND expires_at > now() ORDER BY created_at DESC LIMIT 100"
                    ),
                    {"ws": ws},
                )
            ).all()
        return [InviteView(UUID(str(r[0])), str(r[1]), Role(r[2]), r[3], r[4]) for r in rows]

    async def revoke_invite(self, ws: WorkspaceId, invite_id: UUID) -> bool:
        async with self._uow() as s:
            result = await s.execute(
                text(
                    "UPDATE invitations SET revoked_at = now() WHERE id = :i AND workspace_id = :ws"
                    " AND accepted_at IS NULL AND revoked_at IS NULL"
                ),
                {"i": invite_id, "ws": ws},
            )
        return int(getattr(result, "rowcount", 0) or 0) > 0

    async def find_invite(self, token_hash: bytes) -> InviteRecord | None:
        async with self._uow() as s:
            r = (
                await s.execute(
                    text(
                        "SELECT i.id, i.workspace_id, w.name, i.email_canonical, i.role,"
                        " i.expires_at, (i.accepted_at IS NULL AND i.revoked_at IS NULL)"
                        " FROM invitations i JOIN workspaces w ON w.id = i.workspace_id"
                        " WHERE i.token_hash = :t"
                    ),
                    {"t": token_hash},
                )
            ).first()
        if r is None:
            return None
        return InviteRecord(
            UUID(str(r[0])), WorkspaceId(r[1]), str(r[2]), str(r[3]), Role(r[4]), r[5], bool(r[6])
        )

    async def accept_invite(self, invite_id: UUID, user: UserId) -> bool:
        async with self._uow() as s:
            row = (
                await s.execute(
                    text(
                        "UPDATE invitations SET accepted_at = now() WHERE id = :i"
                        " AND accepted_at IS NULL AND revoked_at IS NULL AND expires_at > now()"
                        " RETURNING workspace_id, role"
                    ),
                    {"i": invite_id},
                )
            ).first()
            if row is None:
                return False
            await s.execute(
                text(
                    "INSERT INTO memberships (workspace_id, user_id, role) VALUES (:ws, :u, :r)"
                    " ON CONFLICT (workspace_id, user_id) DO NOTHING"
                ),
                {"ws": row[0], "u": user, "r": row[1]},
            )
        return True

    async def set_role(self, ws: WorkspaceId, user: UserId, role: Role) -> bool:
        async with self._uow() as s:
            result = await s.execute(
                text(
                    "UPDATE memberships SET role = :r WHERE workspace_id = :ws AND user_id = :u"
                    " AND role <> 'owner'"
                ),
                {"r": role.value, "ws": ws, "u": user},
            )
        return int(getattr(result, "rowcount", 0) or 0) > 0

    async def remove_member(self, ws: WorkspaceId, user: UserId) -> int:
        async with self._uow() as s:
            role = (
                await s.execute(
                    text(
                        "SELECT role FROM memberships WHERE workspace_id = :ws AND user_id = :u"
                        " FOR UPDATE"
                    ),
                    {"ws": ws, "u": user},
                )
            ).scalar_one_or_none()
            if role is None or role == "owner":
                return 0  # an owner is never removed (and never loses their data this way)
            gone = int(
                (
                    await s.execute(
                        text(
                            "SELECT count(*) FROM sources WHERE workspace_id = :ws"
                            " AND created_by = :u AND connector <> 'upload'"
                        ),
                        {"ws": ws, "u": user},
                    )
                ).scalar_one()
            )
            for statement in REMOVE_MEMBER_SQL.split(";"):
                if statement.strip():
                    await s.execute(text(statement), {"ws": ws, "u": user})
            await s.execute(
                text(
                    "DELETE FROM memberships WHERE workspace_id = :ws AND user_id = :u"
                    " AND role <> 'owner'"
                ),
                {"ws": ws, "u": user},
            )
        return gone

    async def set_shared(
        self, ws: WorkspaceId, source_id: SourceId, owner: UserId, shared: bool
    ) -> bool:
        visibility = [user_principal(owner)] + ([workspace_principal(ws)] if shared else [])
        async with self._uow() as s:
            row = (
                await s.execute(
                    text(
                        "UPDATE sources SET visibility = CAST(:v AS text[]) WHERE id = :s"
                        " AND workspace_id = :ws AND created_by = :u AND connector <> 'upload'"
                        " RETURNING id"
                    ),
                    {"v": visibility, "s": source_id, "ws": ws, "u": owner},
                )
            ).first()
            if row is None:
                return False
            # Same transaction: the ACL on documents (and, by trigger, chunks) never lags.
            await s.execute(
                text(
                    "UPDATE documents SET acl = CAST(:v AS text[]), updated_at = now()"
                    " WHERE source_id = :s AND acl IS DISTINCT FROM CAST(:v AS text[])"
                ),
                {"v": visibility, "s": source_id},
            )
        return True

    async def audit(
        self,
        ws: WorkspaceId,
        actor: UserId,
        action: str,
        *,
        target_type: str,
        target_id: str,
        data: dict[str, object] | None = None,
    ) -> None:
        payload: Any = json.dumps(data or {})
        async with self._uow() as s:
            await s.execute(
                text(
                    "INSERT INTO audit_log (workspace_id, actor_id, action, target_type,"
                    " target_id, data) VALUES (:ws, :a, :act, :tt, :ti, CAST(:d AS jsonb))"
                ),
                {
                    "ws": ws,
                    "a": actor,
                    "act": action,
                    "tt": target_type,
                    "ti": target_id,
                    "d": payload,
                },
            )
