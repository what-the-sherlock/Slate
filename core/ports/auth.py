"""Auth ports: accounts, sessions, one-time codes, throttling, hashing, bot checks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from core.domain.auth import CodeCheck, CodePurpose, Role
from core.domain.ids import SessionId, UserId, WorkspaceId


@dataclass(frozen=True, slots=True)
class UserRecord:
    id: UserId
    email: str
    name: str
    password_hash: str
    email_verified: bool
    disabled: bool


@dataclass(frozen=True, slots=True)
class SessionRecord:
    id: SessionId
    user_id: UserId
    workspace_id: WorkspaceId
    role: Role
    email: str
    name: str
    email_verified: bool
    csrf_secret: str
    expires_at: datetime
    last_seen_at: datetime
    group_ids: tuple[str, ...]


class AccountStore(Protocol):
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
        """Create an unverified user, their personal workspace and their owner membership,
        atomically (ADR-0021).

        Raises Conflict if an account already uses this canonical email, including when two
        sign-ups race (the database's unique index decides, not a prior SELECT).
        """
        ...

    async def find_user(self, email_canonical: str) -> UserRecord | None:
        """The account for this canonical email (``canonical_email``), if any."""
        ...

    async def mark_email_verified(self, user_id: UserId) -> None:
        """Idempotent: keeps the first verification time."""
        ...

    async def set_password(self, user_id: UserId, password_hash: str) -> None: ...

    async def replace_password_hash(self, user_id: UserId, old: str, new: str) -> bool:
        """Compare-and-set: store ``new`` only if the hash is still ``old``. Used by the
        sign-in rehash, so a password reset that lands meanwhile is never undone."""
        ...

    async def delete_unverified_before(self, cutoff: datetime) -> int:
        """Delete never-verified accounts created before ``cutoff``, with their personal
        workspaces (cascade). Returns how many accounts were deleted."""
        ...


class SessionStore(Protocol):
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
        """Create a session and return it resolved, as ``get`` would, in one round trip.

        None if the user can't hold a session (disabled or not a member); such a row is
        never returned by ``get`` either, so it can't be used.
        """
        ...

    async def get(self, token_hash: bytes) -> SessionRecord | None:
        """The live (unexpired, user enabled, member) session for this token, if any."""
        ...

    async def touch(self, session_id: SessionId, ttl: timedelta) -> None:
        """Slide expiry forward and record activity."""
        ...

    async def delete(self, session_id: SessionId) -> None: ...

    async def delete_for_user(self, user_id: UserId) -> None: ...

    async def set_workspace(self, session_id: SessionId, ws: WorkspaceId) -> bool:
        """Switch the session's active workspace; False unless its user is a member there."""
        ...


@dataclass(frozen=True, slots=True)
class ThrottleLimits:
    window: timedelta
    per_email_ip: int
    per_ip: int
    per_email: int


class LoginThrottle(Protocol):
    async def begin_attempt(self, *, email: str, ip: str, limits: ThrottleLimits) -> int | None:
        """Atomically check the limits and, if under them, record a *failed* attempt.

        Returns the attempt id, or None if a limit is reached. Recording before the
        password check means concurrent guesses can't all see "under the limit".
        """
        ...

    async def mark_success(self, attempt_id: int) -> None:
        """Turn a reserved attempt into a success (it then stops counting)."""
        ...


class CodeStore(Protocol):
    """One-time codes, stored only as keyed hashes (``hash_code``)."""

    async def replace(
        self, user_id: UserId, purpose: CodePurpose, code_hash: bytes, ttl: timedelta
    ) -> None:
        """Store a new code, invalidating any earlier live code for this user and purpose
        (so only the most recent email works)."""
        ...

    async def check(
        self, user_id: UserId, purpose: CodePurpose, code_hash: bytes, *, max_attempts: int
    ) -> CodeCheck:
        """Atomically compare against the live code: on a match consume it (single use);
        on a miss count the attempt; refuse once ``max_attempts`` misses are reached."""
        ...


class HumanVerifier(Protocol):
    """A bot check (Cloudflare Turnstile) on the endpoints that create accounts or send
    mail. Login doesn't use it, so an outage there can't lock existing users out."""

    async def verify(self, token: str, ip: str | None) -> bool:
        """True if the token proves a human solved the challenge just now.

        Raises UpstreamUnavailable if the check can't be made; callers fail closed.
        """
        ...


class PasswordHasher(Protocol):
    def hash(self, password: str) -> str: ...

    def verify(self, password_hash: str, password: str) -> bool: ...

    def needs_rehash(self, password_hash: str) -> bool: ...
