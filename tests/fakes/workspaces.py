"""In-memory WorkspaceStore over the fake accounts and sources (ADR-0030)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from uuid import UUID, uuid4

from core.domain.auth import Role
from core.domain.ids import SourceId, UserId, WorkspaceId
from core.domain.principals import user_principal, workspace_principal
from core.ports.workspaces import InviteRecord, InviteView, MemberView, WorkspaceMembership
from tests.fakes.auth import InMemoryAccounts
from tests.fakes.sources import InMemorySources
from tests.fakes.system import FakeClock


@dataclass
class _Invite:
    id: UUID
    ws: WorkspaceId
    email: str
    role: Role
    token_hash: bytes
    created_at: datetime
    expires_at: datetime
    accepted: bool = False
    revoked: bool = False


class InMemoryWorkspaces:
    def __init__(
        self, accounts: InMemoryAccounts, sources: InMemorySources, clock: FakeClock
    ) -> None:
        self._acc = accounts
        self._sources = sources
        self._clock = clock
        self.invitations: dict[UUID, _Invite] = {}
        self.audit_log: list[tuple[WorkspaceId, str, str]] = []
        self.chats_deleted: list[tuple[WorkspaceId, UserId]] = []

    def _roles(self, ws: WorkspaceId) -> dict[UserId, Role]:
        out: dict[UserId, Role] = {}
        for uid in self._acc.users:
            role = self._acc.role_in(uid, ws)
            if role is not None:
                out[uid] = role
        return out

    async def memberships(self, user: UserId) -> list[WorkspaceMembership]:
        spaces = [w for w in self._acc.workspaces if self._acc.role_in(user, w) is not None]
        return [
            WorkspaceMembership(
                w,
                self._acc.workspaces[w],
                self._acc.role_in(user, w) or Role.VIEWER,
                len(self._roles(w)),
            )
            for w in spaces
        ]

    async def role_of(self, ws: WorkspaceId, user: UserId) -> Role | None:
        return self._acc.role_in(user, ws)

    async def members(self, ws: WorkspaceId) -> list[MemberView]:
        return [
            MemberView(u, self._acc.users[u].email, self._acc.users[u].name, r, self._clock.now())
            for u, r in self._roles(ws).items()
        ]

    async def member_count(self, ws: WorkspaceId) -> int:
        return len(self._roles(ws))

    async def workspace_name(self, ws: WorkspaceId) -> str | None:
        return self._acc.workspaces.get(ws)

    async def create_invite(
        self, ws: WorkspaceId, *, email_canonical: str, role: Role, token_hash: bytes,
        invited_by: UserId, expires_at: datetime,
    ) -> UUID:  # fmt: skip
        for inv in self.invitations.values():
            if inv.ws == ws and inv.email == email_canonical and not (inv.accepted or inv.revoked):
                inv.revoked = True
        iid = uuid4()
        self.invitations[iid] = _Invite(
            iid, ws, email_canonical, role, token_hash, self._clock.now(), expires_at
        )
        return iid

    async def invites(self, ws: WorkspaceId) -> list[InviteView]:
        return [
            InviteView(i.id, i.email, i.role, i.created_at, i.expires_at)
            for i in self.invitations.values()
            if i.ws == ws and not (i.accepted or i.revoked) and i.expires_at > self._clock.now()
        ]

    async def revoke_invite(self, ws: WorkspaceId, invite_id: UUID) -> bool:
        inv = self.invitations.get(invite_id)
        if inv is None or inv.ws != ws or inv.accepted or inv.revoked:
            return False
        inv.revoked = True
        return True

    async def find_invite(self, token_hash: bytes) -> InviteRecord | None:
        for i in self.invitations.values():
            if i.token_hash == token_hash:
                return InviteRecord(i.id, i.ws, self._acc.workspaces[i.ws], i.email, i.role,
                                    i.expires_at, not (i.accepted or i.revoked))  # fmt: skip
        return None

    async def accept_invite(self, invite_id: UUID, user: UserId) -> bool:
        inv = self.invitations[invite_id]
        if inv.accepted or inv.revoked or inv.expires_at <= self._clock.now():
            return False
        inv.accepted = True
        self._acc.extra.setdefault(user, {})[inv.ws] = inv.role
        return True

    async def set_role(self, ws: WorkspaceId, user: UserId, role: Role) -> bool:
        if user in self._acc.extra and ws in self._acc.extra[user]:
            self._acc.extra[user][ws] = role
            return True
        return False

    async def remove_member(self, ws: WorkspaceId, user: UserId) -> int:
        if self._acc.role_in(user, ws) in (None, Role.OWNER):
            return 0
        mine = [
            r.id
            for r in self._sources.records.values()
            if r.workspace_id == ws and r.created_by == user and r.connector != "upload"
        ]
        for sid in mine:
            await self._sources.delete_source(ws, sid)
        self.chats_deleted.append((ws, user))
        self._acc.extra.get(user, {}).pop(ws, None)
        return len(mine)

    async def set_shared(
        self, ws: WorkspaceId, source_id: SourceId, owner: UserId, shared: bool
    ) -> bool:
        r = self._sources.records.get(source_id)
        if r is None or r.workspace_id != ws or r.created_by != owner or r.connector == "upload":
            return False
        vis = (user_principal(owner),) + ((workspace_principal(ws),) if shared else ())
        self._sources.records[source_id] = replace(r, visibility=vis)
        return True

    async def audit(self, ws: WorkspaceId, actor: UserId, action: str, *, target_type: str,
                    target_id: str, data: dict[str, object] | None = None) -> None:  # fmt: skip
        self.audit_log.append((ws, action, target_id))
