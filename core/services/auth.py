"""Authentication use cases: first-run setup, login, guest access, sessions.

Security properties (SECURITY.md; reviewed 2026-11-24):
* setup needs a one-time setup code from the environment (required in prod), so the
  first visitor to a fresh deploy can't claim the workspace;
* login failures return one generic message, and an unknown email still costs a full
  password verification, so neither text nor timing reveals which emails exist;
* each attempt is recorded *before* the password check, atomically with the limit
  check, so parallel guesses can't all slip under the limit;
* limits are per (email, IP), per IP and (high) per email, so a stranger can't lock
  the owner out; an unknown IP shares one bucket rather than being unlimited;
* every login creates a fresh session (no fixation); only token hashes are stored;
* guest sessions are capped globally.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from uuid import UUID

from core.domain.auth import (
    LOGIN_WINDOW,
    MAX_ACTIVE_GUEST_SESSIONS,
    MAX_FAILURES_PER_EMAIL,
    MAX_FAILURES_PER_EMAIL_IP,
    MAX_FAILURES_PER_IP,
    SESSION_TOUCH_EVERY,
    SESSION_TTL,
    UNKNOWN_IP,
    Identity,
    hash_token,
    new_token,
    normalise_email,
    tokens_match,
    validate_password,
)
from core.domain.errors import Conflict, RateLimited, Unauthenticated, ValidationFailed
from core.domain.ids import GroupId, UserId
from core.domain.principals import Principals
from core.ports.auth import (
    AccountStore,
    LoginThrottle,
    PasswordHasher,
    SessionStore,
    ThrottleLimits,
)
from core.ports.system import Clock

GENERIC_LOGIN_ERROR = "Email or password is incorrect."
GUEST_THROTTLE_KEY = "guest@slate.invalid"
SETUP_THROTTLE_KEY = "setup@slate.invalid"
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

LOGIN_LIMITS = ThrottleLimits(
    window=LOGIN_WINDOW,
    per_email_ip=MAX_FAILURES_PER_EMAIL_IP,
    per_ip=MAX_FAILURES_PER_IP,
    per_email=MAX_FAILURES_PER_EMAIL,
)
# Guest sign-ins count against the IP only (the shared key must not trip per-email limits).
GUEST_LIMITS = ThrottleLimits(
    window=LOGIN_WINDOW,
    per_email_ip=MAX_FAILURES_PER_IP,
    per_ip=MAX_FAILURES_PER_IP,
    per_email=10**9,
)


@dataclass(frozen=True, slots=True)
class IssuedSession:
    """Raw tokens for the cookies. The session token is never stored server-side."""

    session_token: str
    csrf_token: str


@dataclass(frozen=True, slots=True)
class SetupRequest:
    workspace_name: str
    name: str
    email: str
    password: str
    setup_token: str = ""


class AuthService:
    def __init__(
        self,
        accounts: AccountStore,
        sessions: SessionStore,
        throttle: LoginThrottle,
        hasher: PasswordHasher,
        clock: Clock,
        *,
        setup_token: str | None = None,
        setup_token_required: bool = False,
    ) -> None:
        self._accounts = accounts
        self._sessions = sessions
        self._throttle = throttle
        self._hasher = hasher
        self._clock = clock
        self._setup_token = setup_token or None
        self._setup_disabled = setup_token_required and self._setup_token is None
        # Precomputed so the first unknown-email login isn't measurably slower.
        self._dummy_hash = hasher.hash(new_token())

    @property
    def setup_requires_token(self) -> bool:
        return self._setup_token is not None

    async def is_set_up(self) -> bool:
        return await self._accounts.is_set_up()

    async def setup(
        self, req: SetupRequest, *, ip: str | None, user_agent: str | None
    ) -> IssuedSession:
        """Create the workspace and its owner. Only possible while no owner exists."""
        if await self._accounts.is_set_up():
            raise Conflict("This workspace is already set up. Sign in instead.")
        if self._setup_disabled:
            raise Conflict("Setup is disabled until SETUP_TOKEN is configured on the server.")
        attempt = await self._reserve(SETUP_THROTTLE_KEY, ip, LOGIN_LIMITS)
        errors: dict[str, str] = {}
        if self._setup_token is not None and not tokens_match(req.setup_token, self._setup_token):
            errors["setup_token"] = "That setup code isn't right."  # noqa: S105 (message, not a secret)
        email = normalise_email(req.email)
        name, workspace_name = req.name.strip(), req.workspace_name.strip()
        if not _EMAIL.match(email):
            errors["email"] = "Enter an email like name@company.com."
        if not name:
            errors["name"] = "Enter your name."
        if not workspace_name:
            errors["workspace_name"] = "Enter a workspace name."
        if problem := validate_password(req.password):
            errors["password"] = problem
        if errors:
            raise ValidationFailed("Check the highlighted fields.", extra={"fields": errors})
        password_hash = await asyncio.to_thread(self._hasher.hash, req.password)
        _, user_id = await self._accounts.create_workspace_with_owner(
            workspace_name=workspace_name,
            slug=_slugify(workspace_name),
            email=email,
            name=name,
            password_hash=password_hash,
        )
        await self._throttle.mark_success(attempt)
        return await self._issue(user_id, ip=ip, user_agent=user_agent)

    async def login(
        self, email: str, password: str, *, ip: str | None, user_agent: str | None
    ) -> IssuedSession:
        email = normalise_email(email)
        attempt = await self._reserve(email, ip, LOGIN_LIMITS)
        user = await self._accounts.find_user_by_email(email)
        # Always verify against *some* hash so unknown emails take as long as known ones.
        stored = user.password_hash if user and user.password_hash else self._dummy_hash
        ok = await asyncio.to_thread(self._hasher.verify, stored, password)
        if not ok or user is None or user.is_guest or user.disabled or not user.password_hash:
            raise Unauthenticated(GENERIC_LOGIN_ERROR)  # the reserved attempt stays a failure
        await self._throttle.mark_success(attempt)
        return await self._issue(user.id, ip=ip, user_agent=user_agent)

    async def login_as_guest(self, *, ip: str | None, user_agent: str | None) -> IssuedSession:
        # Counted as an attempt, so one IP can't mint unlimited guest sessions.
        await self._reserve(GUEST_THROTTLE_KEY, ip, GUEST_LIMITS)
        guest = await self._accounts.find_guest()
        if guest is None:
            raise Conflict("Guest access isn't available until the workspace is set up.")
        if await self._sessions.count_active_for_user(guest[0].id) >= MAX_ACTIVE_GUEST_SESSIONS:
            raise RateLimited(
                "Guest access is busy right now. Try again later.", retry_after_s=3600
            )
        return await self._issue(guest[0].id, ip=ip, user_agent=user_agent)

    async def authenticate(self, session_token: str | None) -> Identity | None:
        """Resolve a session cookie to an Identity, or None if absent, unknown or expired."""
        if not session_token:
            return None
        record = await self._sessions.get(hash_token(session_token))
        if record is None:
            return None
        if self._clock.now() - record.last_seen_at >= SESSION_TOUCH_EVERY:
            await self._sessions.touch(record.id, SESSION_TTL)
        groups = [GroupId(UUID(g)) for g in record.group_ids]
        return Identity(
            user_id=record.user_id,
            workspace_id=record.workspace_id,
            session_id=record.id,
            email=record.email,
            name=record.name,
            role=record.role,
            is_guest=record.is_guest,
            principals=Principals.for_user(
                record.user_id, record.workspace_id, groups, is_guest=record.is_guest
            ),
            csrf_secret=record.csrf_secret,
        )

    async def logout(self, identity: Identity) -> None:
        await self._sessions.delete(identity.session_id)

    async def _reserve(self, key: str, ip: str | None, limits: ThrottleLimits) -> int:
        attempt = await self._throttle.begin_attempt(email=key, ip=ip or UNKNOWN_IP, limits=limits)
        if attempt is None:
            raise RateLimited(
                "Too many attempts. Try again in 15 minutes.",
                retry_after_s=int(limits.window.total_seconds()),
            )
        return attempt

    async def _issue(
        self, user_id: UserId, *, ip: str | None, user_agent: str | None
    ) -> IssuedSession:
        token, csrf = new_token(), new_token()
        await self._sessions.create(
            user_id=user_id,
            token_hash=hash_token(token),
            csrf_secret=csrf,
            ttl=SESSION_TTL,
            ip=ip,
            user_agent=(user_agent or "")[:400] or None,
        )
        return IssuedSession(session_token=token, csrf_token=csrf)


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:48] or "workspace"
