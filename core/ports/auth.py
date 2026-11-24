"""Auth ports: account storage, sessions, login throttling and password hashing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from core.domain.auth import Role
from core.domain.ids import SessionId, UserId, WorkspaceId


@dataclass(frozen=True, slots=True)
class UserRecord:
    id: UserId
    email: str
    name: str
    password_hash: str | None
    is_guest: bool
    disabled: bool


@dataclass(frozen=True, slots=True)
class SessionRecord:
    id: SessionId
    user_id: UserId
    workspace_id: WorkspaceId
    role: Role
    email: str
    name: str
    is_guest: bool
    csrf_secret: str
    expires_at: datetime
    last_seen_at: datetime
    group_ids: tuple[str, ...]


class AccountStore(Protocol):
    async def is_set_up(self) -> bool:
        """True once a workspace owner exists."""
        ...

    async def create_workspace_with_owner(
        self, *, workspace_name: str, slug: str, email: str, name: str, password_hash: str
    ) -> tuple[WorkspaceId, UserId]:
        """Create the workspace, its owner and its read-only guest, atomically.

        Raises Conflict if an owner already exists (concurrent setups can't both win).
        """
        ...

    async def find_user_by_email(self, email: str) -> UserRecord | None: ...

    async def find_guest(self) -> tuple[UserRecord, WorkspaceId] | None: ...

    async def workspace_for_user(self, user_id: UserId) -> tuple[WorkspaceId, Role] | None: ...


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
    ) -> SessionId: ...

    async def get(self, token_hash: bytes) -> SessionRecord | None:
        """The live (unexpired, user enabled, member) session for this token, if any."""
        ...

    async def touch(self, session_id: SessionId, ttl: timedelta) -> None:
        """Slide expiry forward and record activity."""
        ...

    async def delete(self, session_id: SessionId) -> None: ...

    async def delete_for_user(self, user_id: UserId) -> None: ...

    async def count_active_for_user(self, user_id: UserId) -> int: ...


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


class PasswordHasher(Protocol):
    def hash(self, password: str) -> str: ...

    def verify(self, password_hash: str, password: str) -> bool: ...

    def needs_rehash(self, password_hash: str) -> bool: ...
