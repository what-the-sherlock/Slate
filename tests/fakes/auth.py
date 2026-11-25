from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from core.domain.auth import CodeCheck, CodePurpose, Role
from core.domain.emails import OutgoingEmail
from core.domain.errors import Conflict, UpstreamUnavailable
from core.domain.ids import SessionId, UserId, WorkspaceId
from core.ports.auth import SessionRecord, ThrottleLimits, UserRecord
from tests.fakes.system import FakeClock, SequentialIds


class FakeHasher:
    """Instant and deterministic. Real Argon2 is exercised in its own adapter test."""

    def hash(self, password: str) -> str:
        return f"fake${password[::-1]}"

    def verify(self, password_hash: str, password: str) -> bool:
        return password_hash == self.hash(password)

    def needs_rehash(self, password_hash: str) -> bool:
        return False


@dataclass
class _Member:
    workspace_id: WorkspaceId
    role: Role


class InMemoryAccounts:
    def __init__(self, ids: SequentialIds | None = None, clock: FakeClock | None = None) -> None:
        self.ids = ids or SequentialIds()
        self.clock = clock or FakeClock()
        self.users: dict[UserId, UserRecord] = {}
        self.canonical: dict[UserId, str] = {}
        self.created_at: dict[UserId, datetime] = {}
        self.members: dict[UserId, _Member] = {}
        self.groups: dict[UserId, tuple[str, ...]] = {}
        self.workspaces: dict[WorkspaceId, str] = {}

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
        if email_canonical in self.canonical.values():
            raise Conflict("email taken")
        ws = WorkspaceId(self.ids.new_uuid())
        self.workspaces[ws] = workspace_name
        uid = self.add_user(email, name, password_hash, canonical=email_canonical)
        self.members[uid] = _Member(ws, Role.OWNER)
        return ws, uid

    def add_user(
        self,
        email: str,
        name: str,
        password_hash: str,
        *,
        canonical: str | None = None,
        verified: bool = False,
    ) -> UserId:
        uid = UserId(self.ids.new_uuid())
        self.users[uid] = UserRecord(uid, email, name, password_hash, verified, disabled=False)
        self.canonical[uid] = canonical or email
        self.created_at[uid] = self.clock.now()
        return uid

    def disable(self, uid: UserId) -> None:
        self.users[uid] = replace(self.users[uid], disabled=True)

    async def find_user(self, email_canonical: str) -> UserRecord | None:
        return next(
            (self.users[u] for u, c in self.canonical.items() if c == email_canonical), None
        )

    async def mark_email_verified(self, user_id: UserId) -> None:
        self.users[user_id] = replace(self.users[user_id], email_verified=True)

    async def set_password(self, user_id: UserId, password_hash: str) -> None:
        self.users[user_id] = replace(self.users[user_id], password_hash=password_hash)

    async def delete_unverified_before(self, cutoff: datetime) -> int:
        doomed = [
            u
            for u, rec in self.users.items()
            if not rec.email_verified and self.created_at[u] < cutoff
        ]
        for u in doomed:
            member = self.members.pop(u, None)
            if member is not None:
                self.workspaces.pop(member.workspace_id, None)
            for table in (self.users, self.canonical, self.created_at, self.groups):
                table.pop(u, None)
        return len(doomed)


class InMemorySessions:
    def __init__(self, accounts: InMemoryAccounts, clock: FakeClock) -> None:
        self._accounts = accounts
        self._clock = clock
        self._ids = SequentialIds()
        self.rows: dict[SessionId, dict[str, object]] = {}
        self.touches = 0
        self.creates = 0
        self.gets = 0

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
        sid = SessionId(self._ids.new_uuid())
        now = self._clock.now()
        self.rows[sid] = {
            "user_id": user_id, "token_hash": token_hash, "csrf": csrf_secret,
            "expires_at": now + ttl, "last_seen_at": now,
        }  # fmt: skip
        self.creates += 1
        return await self._resolve(token_hash)

    async def get(self, token_hash: bytes) -> SessionRecord | None:
        self.gets += 1
        return await self._resolve(token_hash)

    async def _resolve(self, token_hash: bytes) -> SessionRecord | None:
        for sid, row in self.rows.items():
            if row["token_hash"] != token_hash:
                continue
            uid: UserId = row["user_id"]  # type: ignore[assignment]
            user = self._accounts.users[uid]
            member = self._accounts.members.get(uid)
            if user.disabled or member is None or row["expires_at"] <= self._clock.now():  # type: ignore[operator]
                return None
            return SessionRecord(
                id=sid, user_id=uid, workspace_id=member.workspace_id, role=member.role,
                email=user.email, name=user.name, email_verified=user.email_verified,
                csrf_secret=row["csrf"],  # type: ignore[arg-type]
                expires_at=row["expires_at"],  # type: ignore[arg-type]
                last_seen_at=row["last_seen_at"],  # type: ignore[arg-type]
                group_ids=self._accounts.groups.get(uid, ()),
            )  # fmt: skip
        return None

    async def touch(self, session_id: SessionId, ttl: timedelta) -> None:
        self.touches += 1
        now = self._clock.now()
        self.rows[session_id].update(last_seen_at=now, expires_at=now + ttl)

    async def delete(self, session_id: SessionId) -> None:
        self.rows.pop(session_id, None)

    async def delete_for_user(self, user_id: UserId) -> None:
        self.rows = {k: v for k, v in self.rows.items() if v["user_id"] != user_id}


class InMemoryThrottle:
    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock
        # [email, ip, success, at]
        self.attempts: list[list[object]] = []

    async def begin_attempt(self, *, email: str, ip: str, limits: ThrottleLimits) -> int | None:
        cutoff = self._clock.now() - limits.window
        failed = [a for a in self.attempts if not a[2] and a[3] > cutoff]  # type: ignore[operator]
        if (
            sum(1 for a in failed if a[0] == email and a[1] == ip) >= limits.per_email_ip
            or sum(1 for a in failed if a[1] == ip) >= limits.per_ip
            or sum(1 for a in failed if a[0] == email) >= limits.per_email
        ):
            return None
        self.attempts.append([email, ip, False, self._clock.now()])
        return len(self.attempts) - 1

    async def mark_success(self, attempt_id: int) -> None:
        self.attempts[attempt_id][2] = True


@dataclass
class _Code:
    code_hash: bytes
    expires_at: datetime
    attempts: int = 0
    consumed: bool = False


class InMemoryCodes:
    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock
        self.codes: dict[tuple[UserId, CodePurpose], _Code] = {}

    async def replace(
        self, user_id: UserId, purpose: CodePurpose, code_hash: bytes, ttl: timedelta
    ) -> None:
        self.codes[(user_id, purpose)] = _Code(code_hash, self._clock.now() + ttl)

    async def check(
        self, user_id: UserId, purpose: CodePurpose, code_hash: bytes, *, max_attempts: int
    ) -> CodeCheck:
        c = self.codes.get((user_id, purpose))
        if c is None or c.consumed or c.expires_at <= self._clock.now():
            return CodeCheck.EXPIRED
        if c.attempts >= max_attempts:
            return CodeCheck.LOCKED
        if c.code_hash == code_hash:
            c.consumed = True
            return CodeCheck.OK
        c.attempts += 1
        return CodeCheck.WRONG


class FakeHumanVerifier:
    """Accepts the token "human"; ``down`` simulates a Turnstile outage."""

    PASS = "human"

    def __init__(self) -> None:
        self.down = False
        self.calls = 0

    async def verify(self, token: str, ip: str | None) -> bool:
        self.calls += 1
        if self.down:
            raise UpstreamUnavailable("turnstile down")
        return token == self.PASS


@dataclass
class FakeMailer:
    """Captures mail instead of sending it. ``down`` simulates an SMTP outage."""

    sent: list[OutgoingEmail] = field(default_factory=list[OutgoingEmail])
    down: bool = False

    async def send(self, email: OutgoingEmail) -> None:
        if self.down:
            raise UpstreamUnavailable("smtp down")
        self.sent.append(email)

    def last_code(self) -> str:
        """The 6-digit code from the most recent email (tests only)."""
        return self.sent[-1].subject.split()[0]
