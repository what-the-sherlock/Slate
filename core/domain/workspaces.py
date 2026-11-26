"""Shared workspaces (ADR-0030): who may invite, change roles, remove and leave. Pure.

The ChatGPT-style model the owner chose: an account has its own workspace and may join
others by invitation; data is private to whoever connected it until they share it
(ADR-0026); leaving removes everything you connected there (owner decision, 2026-11-26).
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from core.domain.auth import Role

INVITE_TTL = timedelta(days=14)
MAX_MEMBERS = 50
INVITES_PER_DAY = 20  # per workspace
INVITABLE = (Role.ADMIN, Role.MEMBER, Role.VIEWER)  # nobody is invited as owner


def new_invite_token() -> tuple[str, bytes]:
    """(token for the link, SHA-256 to store). 256 random bits; only the hash is kept."""
    token = secrets.token_urlsafe(32)
    return token, invite_token_hash(token)


def invite_token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def is_invite_token(token: str) -> bool:
    return 20 <= len(token) <= 100 and all(c.isalnum() or c in "-_" for c in token)


def may_invite(inviter: Role, role: Role) -> bool:
    """Owners invite admins, members and viewers; admins only members and viewers."""
    if role not in INVITABLE:
        return False
    if inviter is Role.OWNER:
        return True
    return inviter is Role.ADMIN and role in (Role.MEMBER, Role.VIEWER)


def may_change_role(actor: Role, target: Role, new: Role, *, self_change: bool) -> bool:
    """Only the owner changes roles, never their own, never to or from owner."""
    return actor is Role.OWNER and not self_change and target is not Role.OWNER and new in INVITABLE


def may_remove(actor: Role, target: Role, *, self_remove: bool) -> bool:
    """Anyone but the owner may leave; the owner removes anyone else; an admin removes
    members and viewers."""
    if self_remove:
        return target is not Role.OWNER
    if target is Role.OWNER:
        return False
    if actor is Role.OWNER:
        return True
    return actor is Role.ADMIN and target in (Role.MEMBER, Role.VIEWER)


def invite_email_text(*, inviter: str, workspace: str, link: str, role: Role) -> tuple[str, str]:
    """(subject, plain-text body). Names are trimmed; the link carries the token in its
    fragment, so it never reaches a server log."""
    who = inviter.strip()[:60] or "Someone"
    ws = workspace.strip()[:80] or "a workspace"
    subject = f"{who} invited you to {ws} on Slate"
    body = (
        f"{who} invited you to join {ws} on Slate as {role.value}.\n\n"
        f"Accept the invitation (it expires in {INVITE_TTL.days} days):\n{link}\n\n"
        "Only sources people choose to share with the workspace are visible to its members; "
        "everything you connect stays private to you unless you share it.\n\n"
        "If you didn't expect this, ignore this email."
    )
    return subject, body
