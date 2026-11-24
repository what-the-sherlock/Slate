"""Identity, roles and session rules (pure; no I/O)."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from core.domain.ids import SessionId, UserId, WorkspaceId
from core.domain.principals import Principals

SESSION_TTL = timedelta(days=14)
SESSION_TOUCH_EVERY = timedelta(minutes=5)  # don't write last_seen on every request
TOKEN_BYTES = 32  # 256-bit session and CSRF tokens

LOGIN_WINDOW = timedelta(minutes=15)
# Per (email, IP): stops guessing from one place. Per IP: stops spraying many emails.
# Per email (high): caps a distributed attack without letting one attacker lock out
# the owner, who is usually on a different IP.
MAX_FAILURES_PER_EMAIL_IP = 5
MAX_FAILURES_PER_IP = 20
MAX_FAILURES_PER_EMAIL = 100
MAX_ACTIVE_GUEST_SESSIONS = 500  # caps rows and chat cost from guest sign-ups
UNKNOWN_IP = "0.0.0.0"  # one shared bucket when the client IP is unknown  # noqa: S104
MIN_PASSWORD_LENGTH = 12


class Role(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"


_RANK = {Role.VIEWER: 0, Role.MEMBER: 1, Role.ADMIN: 2, Role.OWNER: 3}


def role_at_least(role: Role, required: Role) -> bool:
    return _RANK[role] >= _RANK[required]


@dataclass(frozen=True, slots=True)
class Identity:
    """Who is making this request, resolved from their session."""

    user_id: UserId
    workspace_id: WorkspaceId
    session_id: SessionId
    email: str
    name: str
    role: Role
    is_guest: bool
    principals: Principals
    csrf_secret: str

    def can(self, required: Role) -> bool:
        return not self.is_guest and role_at_least(self.role, required)


def new_token() -> str:
    """A URL-safe random token for cookies (session, CSRF)."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> bytes:
    """Only this hash is stored, so a leaked DB can't be replayed as a login.

    A fast hash is right here (unlike passwords): the input is 256 random bits, so
    brute force is impossible regardless of hash speed.
    """
    return hashlib.sha256(token.encode()).digest()


def tokens_match(a: str, b: str) -> bool:
    """Constant-time comparison (no timing side channel)."""
    return secrets.compare_digest(a.encode(), b.encode())


def normalise_email(email: str) -> str:
    return email.strip().lower()


def validate_password(password: str) -> str | None:
    """Return a user-facing problem with the password, or None if acceptable.

    Length beats composition rules (NIST SP 800-63B): no forced symbols or digits.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Use at least {MIN_PASSWORD_LENGTH} characters."
    if len(password) > 256:
        return "Use at most 256 characters."
    if password.strip() != password or not password.strip():
        return "Password can't start or end with spaces."
    return None
