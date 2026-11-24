from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta

from core.domain.auth import Role
from core.domain.errors import Conflict
from core.domain.ids import SessionId, UserId, WorkspaceId
from core.ports.auth import SessionRecord, ThrottleLimits, UserRecord
from tests.fakes.system import FakeClock, SequentialIds

GUEST_EMAIL = "guest@slate.invalid"


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
    def __init__(self, ids: SequentialIds | None = None) -> None:
        self.ids = ids or SequentialIds()
        self.users: dict[UserId, UserRecord] = {}
        self.members: dict[UserId, _Member] = {}
        self.groups: dict[UserId, tuple[str, ...]] = {}

    async def is_set_up(self) -> bool:
        return any(m.role is Role.OWNER for m in self.members.values())

    async def create_workspace_with_owner(
        self, *, workspace_name: str, slug: str, email: str, name: str, password_hash: str
    ) -> tuple[WorkspaceId, UserId]:
        if await self.is_set_up():
            raise Conflict("already set up")
        ws = WorkspaceId(self.ids.new_uuid())
        owner = self.add_user(email, name, password_hash)
        guest = self.add_user(GUEST_EMAIL, "Guest", None, is_guest=True)
        self.members[owner] = _Member(ws, Role.OWNER)
        self.members[guest] = _Member(ws, Role.VIEWER)
        return ws, owner

    def add_user(
        self, email: str, name: str, password_hash: str | None, *, is_guest: bool = False
    ) -> UserId:
        uid = UserId(self.ids.new_uuid())
        self.users[uid] = UserRecord(uid, email, name, password_hash, is_guest, disabled=False)
        return uid

    def disable(self, uid: UserId) -> None:
        self.users[uid] = replace(self.users[uid], disabled=True)

    async def find_user_by_email(self, email: str) -> UserRecord | None:
        return next((u for u in self.users.values() if u.email == email), None)

    async def find_guest(self) -> tuple[UserRecord, WorkspaceId] | None:
        for uid, user in self.users.items():
            if user.is_guest and not user.disabled and uid in self.members:
                return user, self.members[uid].workspace_id
        return None

    async def workspace_for_user(self, user_id: UserId) -> tuple[WorkspaceId, Role] | None:
        m = self.members.get(user_id)
        return None if m is None else (m.workspace_id, m.role)


class InMemorySessions:
    def __init__(self, accounts: InMemoryAccounts, clock: FakeClock) -> None:
        self._accounts = accounts
        self._clock = clock
        self._ids = SequentialIds()
        self.rows: dict[SessionId, dict[str, object]] = {}
        self.touches = 0

    async def create(
        self,
        *,
        user_id: UserId,
        token_hash: bytes,
        csrf_secret: str,
        ttl: timedelta,
        ip: str | None,
        user_agent: str | None,
    ) -> SessionId:
        sid = SessionId(self._ids.new_uuid())
        now = self._clock.now()
        self.rows[sid] = {
            "user_id": user_id, "token_hash": token_hash, "csrf": csrf_secret,
            "expires_at": now + ttl, "last_seen_at": now,
        }  # fmt: skip
        return sid

    async def get(self, token_hash: bytes) -> SessionRecord | None:
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
                email=user.email, name=user.name, is_guest=user.is_guest,
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

    async def count_active_for_user(self, user_id: UserId) -> int:
        now = self._clock.now()
        return sum(
            1
            for r in self.rows.values()
            if r["user_id"] == user_id and r["expires_at"] > now  # type: ignore[operator]
        )


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
