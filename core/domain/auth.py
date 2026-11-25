"""Identity, roles, sign-up and session rules (pure; no I/O)."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from core.domain.disposable_domains import DISPOSABLE_DOMAINS
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
UNKNOWN_IP = "0.0.0.0"  # one shared bucket when the client IP is unknown  # noqa: S104
MIN_PASSWORD_LENGTH = 12
MAX_NAME_LENGTH = 100
MAX_EMAIL_LENGTH = 254  # RFC 5321 path limit

# Open sign-up (ADR-0021). Layered so no single limit is the only defence.
SIGNUPS_PER_IP_PER_HOUR = 3
SIGNUPS_PER_DAY = 50

# One-time email codes (verification, password reset).
CODE_DIGITS = 6
CODE_TTL = timedelta(minutes=15)
MAX_CODE_ATTEMPTS = 5  # per code; 5 guesses of 10^6 is a 1-in-200,000 chance
CODE_SENDS_PER_USER_PER_HOUR = 3
CODE_SENDS_PER_USER_PER_DAY = 10
CODE_SENDS_PER_DAY = 250  # under Brevo's free 300/day, so our cap trips first
CODE_CHECKS_PER_IP_PER_HOUR = 30
UNVERIFIED_ACCOUNT_TTL = timedelta(hours=48)  # then the worker sweep deletes it

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_GMAIL_DOMAINS = frozenset({"gmail.com", "googlemail.com"})


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
    principals: Principals
    csrf_secret: str
    # Only verified accounts may spend money (uploads, search embeddings, chat).
    email_verified: bool = False
    # The verified OWNER_EMAIL account: sees the global budget later. Grants no access to
    # anyone else's documents; those are still filtered by workspace and ACL in SQL.
    is_platform_admin: bool = False

    def can(self, required: Role) -> bool:
        return role_at_least(self.role, required)


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


def is_valid_email(email: str) -> bool:
    """ASCII only (SMTP without SMTPUTF8 can't deliver others), and the part before any
    +tag must be non-empty, or "+x@corp.com" would canonicalise to "@corp.com"."""
    if len(email) > MAX_EMAIL_LENGTH or not email.isascii() or not _EMAIL.match(email):
        return False
    return bool(email.rpartition("@")[0].split("+", 1)[0])


def canonical_email(email: str) -> str:
    """The identity of an inbox, so one inbox can't hold many accounts.

    ``+tags`` are dropped everywhere (sub-addressing delivers to the same inbox on almost
    every provider). For Gmail, dots in the local part are ignored too, and googlemail.com
    is gmail.com. The address the user typed is kept separately for sending mail.
    """
    local, _, domain = normalise_email(email).rpartition("@")
    local = local.split("+", 1)[0]
    if domain in _GMAIL_DOMAINS:
        local, domain = local.replace(".", ""), "gmail.com"
    return f"{local}@{domain}"


def is_disposable_email(email: str) -> bool:
    """True for throwaway-inbox providers, including their subdomains."""
    domain = normalise_email(email).rpartition("@")[2]
    parts = domain.split(".")
    return any(".".join(parts[i:]) in DISPOSABLE_DOMAINS for i in range(len(parts) - 1))


class CodePurpose(StrEnum):
    VERIFY_EMAIL = "verify_email"
    RESET_PASSWORD = "reset_password"  # noqa: S105 (a purpose label, not a secret)


class CodeCheck(StrEnum):
    """Outcome of checking a one-time code."""

    OK = "ok"
    WRONG = "wrong"  # counted against the code's attempts
    EXPIRED = "expired"  # none live: expired, used, replaced or never sent
    LOCKED = "locked"  # too many wrong attempts; a new code is needed


def new_code() -> str:
    """A uniformly random 6-digit code (``secrets``, not ``random``)."""
    return f"{secrets.randbelow(10**CODE_DIGITS):0{CODE_DIGITS}d}"


def hash_code(key: bytes, user_id: UserId, purpose: CodePurpose, code: str) -> bytes:
    """A *keyed* hash (HMAC-SHA256) of a one-time code.

    Unlike session tokens, a code has only 10^6 values, so a plain hash in a leaked
    database could be reversed instantly. With a server-side key it can't be. Binding the
    user and purpose means a reset code can never verify an email, or another account.
    """
    message = f"{user_id}:{purpose.value}:{code.strip()}".encode()
    return hmac.new(key, message, hashlib.sha256).digest()


def workspace_name_for(name: str) -> str:
    """The personal workspace's display name. Users are never asked for one."""
    if not name.strip():
        return "My workspace"
    return f"{name.split()[0][:40]}'s workspace"


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
