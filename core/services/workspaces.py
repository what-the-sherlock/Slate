"""Shared workspaces (ADR-0030): invite, accept, switch, change roles, remove, share.

Rules (``core.domain.workspaces``): the owner invites admins, members and viewers; admins
invite members and viewers. An invite is bound to one email address: only the account that
owns that (canonical) address can accept it, so a forwarded link is useless. Accepting adds
the membership and switches the session to it. Leaving or being removed deletes everything
that person connected in that workspace (owner decision). Sharing a source is the act of
its connector alone, and is audited.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

import structlog

from core.domain.auth import Identity, Role, canonical_email
from core.domain.emails import OutgoingEmail, invite_email_html
from core.domain.errors import (
    Conflict,
    NotFound,
    PermissionDenied,
    RateLimited,
    UpstreamUnavailable,
    ValidationFailed,
)
from core.domain.ids import SourceId, UserId, WorkspaceId
from core.domain.workspaces import (
    INVITE_TTL,
    INVITES_PER_DAY,
    MAX_MEMBERS,
    invite_email_text,
    invite_token_hash,
    is_invite_token,
    may_change_role,
    may_invite,
    may_remove,
    new_invite_token,
)
from core.ports.auth import SessionStore
from core.ports.email import EmailSender
from core.ports.rate_limit import Limit, RateLimiter
from core.ports.sources import SourceStore
from core.ports.system import Clock
from core.ports.workspaces import InviteView, MemberView, WorkspaceMembership, WorkspaceStore
from core.services.auth import CODE_SENDS_PER_DAY

log = structlog.get_logger(__name__)

INVITE_LIMIT = Limit(budget=INVITES_PER_DAY, window=timedelta(days=1))
# The same global mail budget as sign-up codes (one counter for everything Slate sends).
MAIL_ALL = ("email:all", Limit(CODE_SENDS_PER_DAY, timedelta(days=1)))
INVALID_INVITE = "This invitation is invalid or has expired. Ask for a new one."
_EMAIL_CHARS = 320


@dataclass(frozen=True, slots=True)
class InviteCreated:
    id: UUID
    link: str
    emailed: bool


class WorkspaceService:
    def __init__(
        self,
        store: WorkspaceStore,
        sessions: SessionStore,
        sources: SourceStore,
        limiter: RateLimiter,
        clock: Clock,
        *,
        mailer: EmailSender | None,
        app_url: str | None,
    ) -> None:
        self._store = store
        self._sessions = sessions
        self._sources = sources
        self._limiter = limiter
        self._clock = clock
        self._mailer = mailer
        self._app_url = (app_url or "").rstrip("/")

    # ------------------------------------------------------------ reading

    async def workspaces(self, identity: Identity) -> list[WorkspaceMembership]:
        return await self._store.memberships(identity.user_id)

    async def members(self, identity: Identity) -> list[MemberView]:
        return await self._store.members(identity.workspace_id)

    async def invites(self, identity: Identity) -> list[InviteView]:
        if not identity.can(Role.ADMIN):
            raise PermissionDenied("Only admins and the owner can see invitations.")
        return await self._store.invites(identity.workspace_id)

    # ------------------------------------------------------------ switching

    async def switch(self, identity: Identity, ws: WorkspaceId) -> None:
        if not await self._sessions.set_workspace(identity.session_id, ws):
            raise NotFound("You aren't a member of that workspace.")

    # ------------------------------------------------------------ invitations

    async def invite(self, identity: Identity, *, email: str, role: Role) -> InviteCreated:
        email = email.strip()
        if not 3 <= len(email) <= _EMAIL_CHARS or "@" not in email:
            raise ValidationFailed("That isn't a valid email address.")
        if not may_invite(identity.role, role):
            raise PermissionDenied("Your role can't invite people with that role.")
        ws = identity.workspace_id
        canonical = canonical_email(email)
        if canonical == canonical_email(identity.email):
            raise ValidationFailed("You're already in this workspace.")
        if any(canonical_email(m.email) == canonical for m in await self._store.members(ws)):
            raise Conflict("That person is already a member.")
        if await self._store.member_count(ws) >= MAX_MEMBERS:
            raise ValidationFailed(f"A workspace can have at most {MAX_MEMBERS} members.")
        if not await self._limiter.try_spend(f"invite:{ws}", 1, INVITE_LIMIT):
            raise RateLimited(
                "Too many invitations today. Try again tomorrow.", retry_after_s=86400
            )
        token, digest = new_invite_token()
        iid = await self._store.create_invite(
            ws, email_canonical=canonical, role=role, token_hash=digest,
            invited_by=identity.user_id, expires_at=self._clock.now() + INVITE_TTL,
        )  # fmt: skip
        link = f"{self._app_url}/invite#{token}" if self._app_url else f"/invite#{token}"
        emailed = await self._send(identity, email, link, role)
        await self._store.audit(
            ws, identity.user_id, "member.invited", target_type="invitation",
            target_id=str(iid), data={"role": role.value, "emailed": emailed},
        )  # fmt: skip
        log.info("workspace.invited", emailed=emailed)
        return InviteCreated(iid, link, emailed)

    async def _send(self, identity: Identity, to: str, link: str, role: Role) -> bool:
        """Mail the link when SMTP and APP_URL are set; otherwise the inviter shares it."""
        if self._mailer is None or not self._app_url:
            return False
        key, limit = MAIL_ALL
        if not await self._limiter.try_spend(key, 1, limit):
            return False  # the day's mail budget is spent: the inviter can still copy the link
        name = await self._store.workspace_name(identity.workspace_id) or "a workspace"
        subject, body = invite_email_text(
            inviter=identity.name, workspace=name, link=link, role=role
        )
        page = invite_email_html(
            inviter=identity.name.strip()[:60] or "Someone", workspace=name.strip()[:80],
            role=role.value, link=link, days=INVITE_TTL.days, app_url=self._app_url,
        )  # fmt: skip
        try:
            await self._mailer.send(OutgoingEmail(to=to, subject=subject, text=body, html=page))
        except UpstreamUnavailable:
            log.warning("workspace.invite_mail_failed")
            return False
        return True

    async def revoke(self, identity: Identity, invite_id: UUID) -> None:
        if not identity.can(Role.ADMIN):
            raise PermissionDenied("Only admins and the owner can revoke invitations.")
        if not await self._store.revoke_invite(identity.workspace_id, invite_id):
            raise NotFound("This invitation doesn't exist.")
        await self._store.audit(
            identity.workspace_id, identity.user_id, "member.invite_revoked",
            target_type="invitation", target_id=str(invite_id),
        )  # fmt: skip

    async def accept(self, identity: Identity, token: str) -> WorkspaceId:
        """Join the invitation's workspace and switch to it. Only the invited address may."""
        if not is_invite_token(token):
            raise ValidationFailed(INVALID_INVITE)
        inv = await self._store.find_invite(invite_token_hash(token))
        if inv is None or not inv.open or inv.expires_at <= self._clock.now():
            raise ValidationFailed(INVALID_INVITE)
        if canonical_email(identity.email) != inv.email_canonical.lower():
            # Same answer for every mismatch: it never confirms who was invited.
            raise PermissionDenied(
                "This invitation is for a different email address. Sign in with the invited one."
            )
        if await self._store.role_of(inv.workspace_id, identity.user_id) is None:
            if await self._store.member_count(inv.workspace_id) >= MAX_MEMBERS:
                raise ValidationFailed(f"That workspace already has {MAX_MEMBERS} members.")
            if not await self._store.accept_invite(inv.id, identity.user_id):
                raise ValidationFailed(INVALID_INVITE)
            await self._store.audit(
                inv.workspace_id, identity.user_id, "member.joined", target_type="user",
                target_id=str(identity.user_id), data={"role": inv.role.value},
            )  # fmt: skip
        await self._sessions.set_workspace(identity.session_id, inv.workspace_id)
        log.info("workspace.joined")
        return inv.workspace_id

    # ------------------------------------------------------------ members

    async def set_role(self, identity: Identity, user: UserId, role: Role) -> None:
        current = await self._store.role_of(identity.workspace_id, user)
        if current is None:
            raise NotFound("That person isn't a member.")
        if not may_change_role(identity.role, current, role, self_change=user == identity.user_id):
            raise PermissionDenied("Only the owner can change roles, and not their own.")
        await self._store.set_role(identity.workspace_id, user, role)
        await self._store.audit(
            identity.workspace_id, identity.user_id, "member.role_changed", target_type="user",
            target_id=str(user), data={"from": current.value, "to": role.value},
        )  # fmt: skip

    async def remove(self, identity: Identity, user: UserId) -> int:
        """Remove a member, or leave (``user`` = yourself). Everything they connected in this
        workspace is deleted with them."""
        target = await self._store.role_of(identity.workspace_id, user)
        if target is None:
            raise NotFound("That person isn't a member.")
        self_remove = user == identity.user_id
        if not may_remove(identity.role, target, self_remove=self_remove):
            raise PermissionDenied(
                "The owner can't leave their own workspace."
                if self_remove
                else "Your role can't remove that member."
            )
        removed = await self._store.remove_member(identity.workspace_id, user)
        await self._store.audit(
            identity.workspace_id, identity.user_id,
            "member.left" if self_remove else "member.removed", target_type="user",
            target_id=str(user), data={"sources_deleted": removed},
        )  # fmt: skip
        log.info("workspace.member_removed", sources=removed, left=self_remove)
        return removed

    # ------------------------------------------------------------ sharing

    async def share(self, identity: Identity, source_id: SourceId, *, shared: bool) -> None:
        """Make a source you connected visible to this workspace's members, or private again."""
        source = await self._sources.get_source(identity.workspace_id, source_id)
        if source is None or source.created_by != identity.user_id:
            # Not yours (or not here): the same 404, never "it exists but isn't yours".
            raise NotFound("This source doesn't exist.")
        if not await self._store.set_shared(
            identity.workspace_id, source_id, identity.user_id, shared
        ):
            raise NotFound("This source doesn't exist.")
        await self._store.audit(
            identity.workspace_id, identity.user_id,
            "source.shared" if shared else "source.unshared", target_type="source",
            target_id=str(source_id),
        )  # fmt: skip
        log.info("workspace.sharing_changed", shared=shared)
