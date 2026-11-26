"""Workspace membership, invitations and sharing (ADR-0030). Every query is scoped by the
workspace the caller is acting in; the service checks roles before calling."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from core.domain.auth import Role
from core.domain.ids import SourceId, UserId, WorkspaceId


@dataclass(frozen=True, slots=True)
class WorkspaceMembership:
    workspace_id: WorkspaceId
    name: str
    role: Role
    members: int


@dataclass(frozen=True, slots=True)
class MemberView:
    user_id: UserId
    email: str
    name: str
    role: Role
    joined_at: datetime


@dataclass(frozen=True, slots=True)
class InviteView:
    id: UUID
    email: str
    role: Role
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class InviteRecord:
    id: UUID
    workspace_id: WorkspaceId
    workspace_name: str
    email_canonical: str
    role: Role
    expires_at: datetime
    open: bool  # neither accepted nor revoked


class WorkspaceStore(Protocol):
    async def memberships(self, user: UserId) -> list[WorkspaceMembership]: ...

    async def role_of(self, ws: WorkspaceId, user: UserId) -> Role | None: ...

    async def members(self, ws: WorkspaceId) -> list[MemberView]: ...

    async def member_count(self, ws: WorkspaceId) -> int: ...

    async def workspace_name(self, ws: WorkspaceId) -> str | None: ...

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
        """Replaces any open invite for that address (a resend gets a fresh link)."""
        ...

    async def invites(self, ws: WorkspaceId) -> list[InviteView]:
        """Open, unexpired invites."""
        ...

    async def revoke_invite(self, ws: WorkspaceId, invite_id: UUID) -> bool: ...

    async def find_invite(self, token_hash: bytes) -> InviteRecord | None: ...

    async def accept_invite(self, invite_id: UUID, user: UserId) -> bool:
        """In one transaction: mark it accepted (only if still open and unexpired) and add the
        membership. False if someone else got there first or it expired meanwhile."""
        ...

    async def set_role(self, ws: WorkspaceId, user: UserId, role: Role) -> bool: ...

    async def remove_member(self, ws: WorkspaceId, user: UserId) -> int:
        """In one transaction: delete everything ``user`` connected or uploaded in ``ws``
        (sources, their documents, credentials, uploads, chats), then the membership.
        Returns how many sources went."""
        ...

    async def set_shared(
        self, ws: WorkspaceId, source_id: SourceId, owner: UserId, shared: bool
    ) -> bool:
        """Widen a source owner's ACL to the workspace, or narrow it back, for the source and
        all its documents (chunks follow by trigger). False if ``owner`` didn't connect it."""
        ...

    async def audit(
        self,
        ws: WorkspaceId,
        actor: UserId,
        action: str,
        *,
        target_type: str,
        target_id: str,
        data: dict[str, object] | None = None,
    ) -> None: ...
