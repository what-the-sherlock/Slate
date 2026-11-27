"""Authentication use cases: sign-up, email verification, login, password reset, sessions.

Open sign-up with a personal workspace per user (ADR-0021). Security properties
(SECURITY.md):

* **Isolation:** every account gets its own workspace and is its owner. Nothing about
  another user is reachable: all reads are filtered by workspace and ACL in SQL.
* **Spam:** sign-up, resend and reset need a human check (Turnstile, fail closed) and pass
  layered rate limits (per IP, global per day), plus a honeypot field. Disposable
  inboxes are refused, and one inbox is one account (``canonical_email``).
* **Spend:** a new account is unverified and can't spend anything until it proves its
  inbox with a 6-digit code. Codes are keyed hashes, single use, 15 minutes, 5 tries.
* **Owner:** the address in ``OWNER_EMAIL`` is the platform admin. Signing up with it
  needs ``SETUP_TOKEN`` where one is configured (required in prod), so a stranger can't
  claim it first on a fresh deploy.
* **Login** (unchanged since the M1 review): one generic failure message, a full
  password verification even for unknown emails, attempts recorded atomically before
  the password check, limits per (email, IP), per IP and per email.
* **Reset** always answers the same way whether or not the account exists, and signs out
  every session of the account it resets.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, replace
from datetime import timedelta
from uuid import UUID

import structlog

from core.domain.admin import AuthEventKind
from core.domain.auth import (
    CODE_CHECKS_PER_IP_PER_HOUR,
    CODE_DIGITS,
    CODE_SENDS_PER_DAY,
    CODE_SENDS_PER_USER_PER_DAY,
    CODE_SENDS_PER_USER_PER_HOUR,
    CODE_TTL,
    LOGIN_WINDOW,
    MAX_CODE_ATTEMPTS,
    MAX_FAILURES_PER_EMAIL,
    MAX_FAILURES_PER_EMAIL_IP,
    MAX_FAILURES_PER_IP,
    MAX_NAME_LENGTH,
    SESSION_TOUCH_EVERY,
    SESSION_TTL,
    SIGNUPS_PER_DAY,
    SIGNUPS_PER_IP_PER_HOUR,
    UNKNOWN_IP,
    CodeCheck,
    CodePurpose,
    Identity,
    canonical_email,
    hash_code,
    hash_token,
    is_disposable_email,
    is_valid_email,
    new_code,
    new_token,
    normalise_email,
    tokens_match,
    validate_password,
    workspace_name_for,
)
from core.domain.emails import code_email
from core.domain.errors import (
    Conflict,
    PermissionDenied,
    RateLimited,
    Unauthenticated,
    UpstreamUnavailable,
    ValidationFailed,
)
from core.domain.ids import GroupId, UserId
from core.domain.principals import Principals
from core.ports.admin import AuthEventLog
from core.ports.auth import (
    AccountStore,
    CodeStore,
    HumanVerifier,
    LoginThrottle,
    PasswordHasher,
    SessionRecord,
    SessionStore,
    ThrottleLimits,
)
from core.ports.email import EmailSender
from core.ports.rate_limit import Limit, RateLimiter
from core.ports.system import Clock
from core.services.maintenance import sweep_unverified_accounts

log = structlog.get_logger(__name__)

GENERIC_LOGIN_ERROR = "Email or password is incorrect."
GENERIC_FIELDS_ERROR = "Check the highlighted fields."
BAD_RESET_CODE = "That code isn't right or has expired. Request a new one."
HUMAN_CHECK_FAILED = "We couldn't confirm you're a person. Refresh the page and try again."
_CODE = re.compile(rf"^\d{{{CODE_DIGITS}}}$")
_HOUR = timedelta(hours=1)
_DAY = timedelta(days=1)

LOGIN_LIMITS = ThrottleLimits(
    window=LOGIN_WINDOW,
    per_email_ip=MAX_FAILURES_PER_EMAIL_IP,
    per_ip=MAX_FAILURES_PER_IP,
    per_email=MAX_FAILURES_PER_EMAIL,
)


@dataclass(frozen=True, slots=True)
class IssuedSession:
    """Raw tokens for the cookies (the session token is never stored server-side), plus
    the resolved identity, so callers don't re-read the session they just created."""

    session_token: str
    csrf_token: str
    identity: Identity


@dataclass(frozen=True, slots=True)
class SignupRequest:
    name: str
    email: str
    password: str
    human_token: str = ""  # Turnstile response from the browser
    setup_token: str = ""  # only for OWNER_EMAIL
    trap: str = ""  # honeypot: a field people never see; bots fill it


@dataclass(frozen=True, slots=True)
class AuthPolicy:
    """Deployment settings for auth, from core/config.py via the composition root."""

    code_key: bytes  # HMAC key for one-time codes (derived from SESSION_SECRET)
    owner_email: str | None = None
    setup_token: str | None = None
    setup_token_required: bool = False  # prod: refuse OWNER_EMAIL sign-up without a token
    signups_enabled: bool = True  # kill switch (SIGNUPS_ENABLED)


class AuthService:
    def __init__(
        self,
        *,
        accounts: AccountStore,
        sessions: SessionStore,
        throttle: LoginThrottle,
        hasher: PasswordHasher,
        codes: CodeStore,
        rate: RateLimiter,
        human: HumanVerifier,
        mailer: EmailSender,
        clock: Clock,
        policy: AuthPolicy,
        events: AuthEventLog,
    ) -> None:
        self._accounts = accounts
        self._sessions = sessions
        self._throttle = throttle
        self._hasher = hasher
        self._codes = codes
        self._rate = rate
        self._human = human
        self._mailer = mailer
        self._clock = clock
        self._policy = policy
        self._events = events
        self._owner = canonical_email(policy.owner_email) if policy.owner_email else None
        self._setup_token = policy.setup_token or None
        # Precomputed so the first unknown-email login isn't measurably slower.
        self._dummy_hash = hasher.hash(new_token())

    @property
    def signups_enabled(self) -> bool:
        return self._policy.signups_enabled

    # ------------------------------------------------------------------ sign-up

    async def signup(
        self, req: SignupRequest, *, ip: str | None, user_agent: str | None
    ) -> IssuedSession:
        """Create an unverified account with its own workspace, sign it in, and email a
        verification code. Mail failure doesn't fail sign-up: the user can resend."""
        if not self._policy.signups_enabled:
            raise PermissionDenied("Sign-ups are closed right now. Try again later.")
        if req.trap:
            raise ValidationFailed(GENERIC_FIELDS_ERROR)  # deliberately vague for bots
        email, name = normalise_email(req.email), " ".join(req.name.split())
        canonical = canonical_email(email) if is_valid_email(email) else ""
        # Cheap checks first, so a typo doesn't burn the human check or a sign-up slot.
        errors: dict[str, str] = {}
        if not name:
            errors["name"] = "Enter your name."
        elif len(name) > MAX_NAME_LENGTH:
            errors["name"] = f"Use at most {MAX_NAME_LENGTH} characters."
        if not canonical:
            errors["email"] = "Enter an email like name@company.com."
        elif is_disposable_email(email):
            errors["email"] = "Use a permanent email address, not a disposable one."
        if problem := validate_password(req.password):
            errors["password"] = problem
        if errors:
            raise ValidationFailed(GENERIC_FIELDS_ERROR, extra={"fields": errors})

        await self._require_human(req.human_token, ip)
        await self._spend(f"signup:ip:{ip or UNKNOWN_IP}", Limit(SIGNUPS_PER_IP_PER_HOUR, _HOUR))
        await self._spend("signup:all", Limit(SIGNUPS_PER_DAY, _DAY))
        # After the rate limits, so the setup code can't be guessed faster than they allow.
        self._check_owner_claim(canonical, req.setup_token)

        password_hash = await asyncio.to_thread(self._hasher.hash, req.password)
        try:
            _, user_id = await self._accounts.create_user_with_workspace(
                email=email,
                email_canonical=canonical,
                name=name,
                password_hash=password_hash,
                workspace_name=workspace_name_for(name),
                slug=f"ws-{new_token()[:12].lower()}",
            )
        except Conflict:
            # Sign-up must say so (there's no other way to tell someone to sign in), so this
            # reveals that the address has an account; the limits above bound how fast
            # anyone can ask (SECURITY.md).
            raise Conflict(
                "An account with this email already exists. Sign in instead.",
                extra={"fields": {"email": "This email already has an account."}},
            ) from None
        session = await self._issue(user_id, ip=ip, user_agent=user_agent)
        await self._try_send_code(user_id, email, name, CodePurpose.VERIFY_EMAIL)
        log.info("auth.signup", user_id=str(user_id))
        await self._event(AuthEventKind.SIGNUP, user_id)
        return session

    def _check_owner_claim(self, canonical: str, setup_token: str) -> None:
        if self._owner is None or canonical != self._owner:
            return
        if self._setup_token is None:
            if self._policy.setup_token_required:
                raise ValidationFailed(
                    GENERIC_FIELDS_ERROR,
                    extra={"fields": {"email": "This address is reserved. Sign in instead."}},
                )
            return  # dev/test without a token: the owner email signs up normally
        if not tokens_match(setup_token, self._setup_token):
            message = (
                "Enter the setup code for this address."
                if not setup_token
                else "That setup code isn't right."
            )
            raise ValidationFailed(GENERIC_FIELDS_ERROR, extra={"fields": {"setup_token": message}})

    # ------------------------------------------------------------- verification

    async def resend_verification(
        self, identity: Identity, *, human_token: str, ip: str | None
    ) -> None:
        if identity.email_verified:
            return
        await self._require_human(human_token, ip)
        await self._send_code(
            identity.user_id, identity.email, identity.name, CodePurpose.VERIFY_EMAIL
        )

    async def verify_email(self, identity: Identity, code: str, *, ip: str | None) -> Identity:
        """Check the code; on success the account may spend. Returns the updated identity."""
        if identity.email_verified:
            return identity
        await self._spend_code_check(ip)
        outcome = await self._check_code(identity.user_id, CodePurpose.VERIFY_EMAIL, code)
        if outcome is not CodeCheck.OK:
            message = {
                CodeCheck.WRONG: "That code isn't right.",
                CodeCheck.EXPIRED: "That code has expired. Send a new one.",
                CodeCheck.LOCKED: "Too many wrong tries. Send a new code.",
            }[outcome]
            raise ValidationFailed(message, extra={"fields": {"code": message}})
        await self._accounts.mark_email_verified(identity.user_id)
        log.info("auth.email_verified", user_id=str(identity.user_id))
        await self._event(AuthEventKind.EMAIL_VERIFIED, identity.user_id)
        return replace(
            identity,
            email_verified=True,
            is_platform_admin=self._is_owner(identity.email, verified=True),
        )

    # ------------------------------------------------------------------- login

    async def login(
        self, email: str, password: str, *, ip: str | None, user_agent: str | None
    ) -> IssuedSession:
        email = normalise_email(email)
        try:
            attempt = await self._reserve(email, ip, LOGIN_LIMITS)
        except RateLimited:
            # One event per IP per window: a script hammering a locked login must not turn
            # every rejected request into a stored row. (An over-limit spend writes nothing.)
            if await self._rate.try_spend(
                f"event:throttled:{ip or UNKNOWN_IP}", 1, Limit(1, LOGIN_WINDOW)
            ):
                await self._event(AuthEventKind.LOGIN_THROTTLED, None)
            raise
        user = (
            await self._accounts.find_user(canonical_email(email))
            if is_valid_email(email)
            else None
        )
        # Always verify against *some* hash so unknown emails take as long as known ones.
        stored = user.password_hash if user else self._dummy_hash
        ok = await asyncio.to_thread(self._hasher.verify, stored, password)
        if not ok or user is None or user.disabled:
            await self._event(AuthEventKind.LOGIN_FAILED, user.id if user else None)
            raise Unauthenticated(GENERIC_LOGIN_ERROR)  # the reserved attempt stays a failure
        await self._throttle.mark_success(attempt)
        await self._upgrade_hash(user.id, user.password_hash, password)
        session = await self._issue(user.id, ip=ip, user_agent=user_agent)
        await self._event(AuthEventKind.LOGIN_SUCCESS, user.id)
        return session

    async def authenticate(self, session_token: str | None) -> Identity | None:
        """Resolve a session cookie to an Identity, or None if absent, unknown or expired."""
        if not session_token:
            return None
        record = await self._sessions.get(hash_token(session_token))
        if record is None:
            return None
        if self._clock.now() - record.last_seen_at >= SESSION_TOUCH_EVERY:
            await self._sessions.touch(record.id, SESSION_TTL)
        return self._identity(record)

    async def logout(self, identity: Identity) -> None:
        await self._sessions.delete(identity.session_id)
        await self._event(AuthEventKind.LOGOUT, identity.user_id)

    # ---------------------------------------------------------- password reset

    async def request_password_reset(self, email: str, *, human_token: str, ip: str | None) -> None:
        """Email a reset code if the account exists. Same answer either way."""
        await self._require_human(human_token, ip)
        await self._spend(
            f"reset:ip:{ip or UNKNOWN_IP}", Limit(CODE_SENDS_PER_USER_PER_HOUR * 2, _HOUR)
        )
        email = normalise_email(email)
        user = (
            await self._accounts.find_user(canonical_email(email))
            if is_valid_email(email)
            else None
        )
        if user is None or user.disabled:
            return
        await self._try_send_code(user.id, user.email, user.name, CodePurpose.RESET_PASSWORD)

    async def reset_password(
        self, email: str, code: str, new_password: str, *, ip: str | None
    ) -> None:
        """Set a new password with a reset code, then sign out every session.

        A reset also proves the inbox, so it verifies the email too.
        """
        await self._spend_code_check(ip)
        if problem := validate_password(new_password):
            raise ValidationFailed(GENERIC_FIELDS_ERROR, extra={"fields": {"password": problem}})
        email = normalise_email(email)
        user = (
            await self._accounts.find_user(canonical_email(email))
            if is_valid_email(email)
            else None
        )
        if user is None or user.disabled:
            raise ValidationFailed(BAD_RESET_CODE, extra={"fields": {"code": BAD_RESET_CODE}})
        if await self._check_code(user.id, CodePurpose.RESET_PASSWORD, code) is not CodeCheck.OK:
            raise ValidationFailed(BAD_RESET_CODE, extra={"fields": {"code": BAD_RESET_CODE}})
        password_hash = await asyncio.to_thread(self._hasher.hash, new_password)
        await self._accounts.set_password(user.id, password_hash)
        await self._accounts.mark_email_verified(user.id)
        await self._sessions.delete_for_user(user.id)  # SECURITY.md: password change → sign out all
        log.info("auth.password_reset", user_id=str(user.id))
        await self._event(AuthEventKind.PASSWORD_RESET, user.id)

    # ------------------------------------------------------------- maintenance

    async def sweep_unverified(self) -> int:
        """Delete accounts never verified within UNVERIFIED_ACCOUNT_TTL (worker sweep)."""
        return await sweep_unverified_accounts(self._accounts, self._clock.now())

    # ----------------------------------------------------------------- helpers

    async def _event(self, kind: AuthEventKind, user_id: UserId | None) -> None:
        """Record a security event for the admin console (ADR-0042). Best effort: a
        failed write is logged, never surfaced, so monitoring can't lock anyone out."""
        try:
            await self._events.record(kind, user_id)
        except Exception as exc:
            log.warning("auth.event_not_recorded", kind=kind.value, error=type(exc).__name__)

    async def _upgrade_hash(self, user_id: UserId, stored: str, password: str) -> None:
        """Rehash with current parameters when the stored hash uses older ones. Sign-in is
        the only moment the plaintext is in hand, so upgrades happen here, silently."""
        if not self._hasher.needs_rehash(stored):
            return
        try:
            fresh = await asyncio.to_thread(self._hasher.hash, password)
            # Compare-and-set: if a reset changed the hash since we verified, keep the reset.
            if await self._accounts.replace_password_hash(user_id, stored, fresh):
                log.info("auth.password_rehashed", user_id=str(user_id))
        except Exception as exc:
            log.warning("auth.rehash_failed", error=type(exc).__name__)

    async def _require_human(self, token: str, ip: str | None) -> None:
        if not token:
            raise PermissionDenied(HUMAN_CHECK_FAILED)
        try:
            ok = await self._human.verify(token, ip)
        except UpstreamUnavailable:
            # Fail closed: without the check these endpoints would be open to bots.
            raise UpstreamUnavailable(
                "We can't confirm you're a person right now. Try again in a minute."
            ) from None
        if not ok:
            raise PermissionDenied(HUMAN_CHECK_FAILED)

    async def _spend(self, key: str, limit: Limit) -> None:
        if not await self._rate.try_spend(key, 1, limit):
            raise RateLimited(
                "Too many attempts. Try again later.",
                retry_after_s=int(limit.window.total_seconds()),
            )

    async def _spend_code_check(self, ip: str | None) -> None:
        await self._spend(
            f"code-check:ip:{ip or UNKNOWN_IP}", Limit(CODE_CHECKS_PER_IP_PER_HOUR, _HOUR)
        )

    async def _check_code(self, user_id: UserId, purpose: CodePurpose, code: str) -> CodeCheck:
        code = code.strip()
        if not _CODE.match(code):
            # Not a guess at all, so it doesn't use up one of the code's attempts.
            return CodeCheck.WRONG
        return await self._codes.check(
            user_id,
            purpose,
            hash_code(self._policy.code_key, user_id, purpose, code),
            max_attempts=MAX_CODE_ATTEMPTS,
        )

    async def _send_code(self, user_id: UserId, to: str, name: str, purpose: CodePurpose) -> None:
        """Issue a fresh code (invalidating the last one) and email it, within the limits:
        per account per hour and per day, and globally per day (the provider's quota)."""
        busy = "We've sent several codes already. Wait a while, then try again."
        for key, limit in (
            (f"email:user:{user_id}:h", Limit(CODE_SENDS_PER_USER_PER_HOUR, _HOUR)),
            (f"email:user:{user_id}:d", Limit(CODE_SENDS_PER_USER_PER_DAY, _DAY)),
            ("email:all", Limit(CODE_SENDS_PER_DAY, _DAY)),
        ):
            if not await self._rate.try_spend(key, 1, limit):
                raise RateLimited(busy, retry_after_s=int(limit.window.total_seconds()))
        code = new_code()
        await self._codes.replace(
            user_id, purpose, hash_code(self._policy.code_key, user_id, purpose, code), CODE_TTL
        )
        await self._mailer.send(code_email(to, name, code, purpose))  # never log `code`

    async def _try_send_code(
        self, user_id: UserId, to: str, name: str, purpose: CodePurpose
    ) -> None:
        """Send where failure must not change the response (sign-up, reset request)."""
        try:
            await self._send_code(user_id, to, name, purpose)
        except (RateLimited, UpstreamUnavailable, ValueError) as exc:
            log.warning("auth.code_not_sent", purpose=purpose.value, error=type(exc).__name__)

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
        record = await self._sessions.create(
            user_id=user_id,
            token_hash=hash_token(token),
            csrf_secret=csrf,
            ttl=SESSION_TTL,
            ip=ip,
            user_agent=(user_agent or "")[:400] or None,
        )
        if record is None:  # disabled or membership removed between the checks and now
            raise Unauthenticated(GENERIC_LOGIN_ERROR)
        return IssuedSession(session_token=token, csrf_token=csrf, identity=self._identity(record))

    def _is_owner(self, email: str, *, verified: bool) -> bool:
        return verified and self._owner is not None and canonical_email(email) == self._owner

    def _identity(self, record: SessionRecord) -> Identity:
        groups = [GroupId(UUID(g)) for g in record.group_ids]
        return Identity(
            user_id=record.user_id,
            workspace_id=record.workspace_id,
            session_id=record.id,
            email=record.email,
            name=record.name,
            role=record.role,
            principals=Principals.for_user(record.user_id, record.workspace_id, groups),
            csrf_secret=record.csrf_secret,
            email_verified=record.email_verified,
            is_platform_admin=self._is_owner(record.email, verified=record.email_verified),
        )
