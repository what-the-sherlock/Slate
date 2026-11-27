from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest

from core.domain.admin import AuthEventKind as K
from core.domain.auth import (
    CODE_SENDS_PER_USER_PER_HOUR,
    CODE_TTL,
    LOGIN_WINDOW,
    MAX_CODE_ATTEMPTS,
    MAX_FAILURES_PER_EMAIL_IP,
    MAX_FAILURES_PER_IP,
    SESSION_TOUCH_EVERY,
    SESSION_TTL,
    SIGNUPS_PER_IP_PER_HOUR,
    UNKNOWN_IP,
    UNVERIFIED_ACCOUNT_TTL,
    CodePurpose,
    Identity,
    Role,
    hash_token,
)
from core.domain.errors import (
    Conflict,
    PermissionDenied,
    RateLimited,
    Unauthenticated,
    UpstreamUnavailable,
    ValidationFailed,
)
from core.domain.principals import PUBLIC
from core.services.auth import (
    BAD_RESET_CODE,
    GENERIC_LOGIN_ERROR,
    AuthPolicy,
    AuthService,
    IssuedSession,
    SignupRequest,
)
from tests.fakes.auth import (
    FakeHasher,
    FakeHumanVerifier,
    FakeMailer,
    InMemoryAccounts,
    InMemoryAuthEvents,
    InMemoryCodes,
    InMemorySessions,
    InMemoryThrottle,
)
from tests.fakes.rate_limit import InMemoryRateLimiter
from tests.fakes.system import FakeClock

PASSWORD = "correct horse battery"
HUMAN = FakeHumanVerifier.PASS
ADA = SignupRequest("Ada Lovelace", "Ada@Acme.dev ", PASSWORD, human_token=HUMAN)


class Env:
    def __init__(self, **policy: object) -> None:
        self.clock = FakeClock()
        self.accounts = InMemoryAccounts(clock=self.clock)
        self.sessions = InMemorySessions(self.accounts, self.clock)
        self.throttle = InMemoryThrottle(self.clock)
        self.hasher = FakeHasher()
        self.codes = InMemoryCodes(self.clock)
        self.rate = InMemoryRateLimiter(self.clock)
        self.human = FakeHumanVerifier()
        self.mailer = FakeMailer()
        self.events = InMemoryAuthEvents(self.clock)
        self.auth = AuthService(
            accounts=self.accounts,
            sessions=self.sessions,
            throttle=self.throttle,
            hasher=self.hasher,
            codes=self.codes,
            rate=self.rate,
            human=self.human,
            mailer=self.mailer,
            clock=self.clock,
            policy=AuthPolicy(code_key=b"k" * 32, **policy),  # type: ignore[arg-type]
            events=self.events,
        )

    async def signup(self, req: SignupRequest = ADA, ip: str | None = "1.2.3.4") -> IssuedSession:
        return await self.auth.signup(req, ip=ip, user_agent="t")

    async def verified(self, req: SignupRequest = ADA) -> Identity:
        issued = await self.signup(req)
        return await self.auth.verify_email(issued.identity, self.mailer.last_code(), ip="1.2.3.4")


@pytest.fixture
def env() -> Env:
    return Env()


async def login(
    env: Env, email: str = "ada@acme.dev", password: str = PASSWORD, ip: str | None = "1.2.3.4"
) -> str:
    return (await env.auth.login(email, password, ip=ip, user_agent="t")).session_token


async def fail(env: Env, *, ip: str | None = "1.2.3.4", email: str = "ada@acme.dev") -> None:
    with pytest.raises(Unauthenticated):
        await login(env, email=email, password="wrong password!", ip=ip)


def fields_of(
    exc: pytest.ExceptionInfo[ValidationFailed] | pytest.ExceptionInfo[Conflict],
) -> dict[str, str]:
    fields = exc.value.extra["fields"]
    assert isinstance(fields, dict)
    return fields


# ---------------------------------------------------------------- sign-up


async def test_signup_creates_a_personal_workspace_owned_by_the_user(env: Env) -> None:
    issued = await env.signup()
    me = issued.identity
    assert me.role is Role.OWNER
    assert me.email == "ada@acme.dev"  # normalised
    assert not me.email_verified
    assert env.accounts.workspaces[me.workspace_id] == "Ada's workspace"  # never asked for
    assert set(me.principals.values) == {
        f"user:{me.user_id}",
        f"workspace:{me.workspace_id}",
        PUBLIC,
    }
    assert await env.auth.authenticate(issued.session_token) == me


async def test_every_signup_gets_its_own_workspace(env: Env) -> None:
    a = await env.signup()
    b = await env.signup(SignupRequest("Bob", "bob@acme.dev", PASSWORD, human_token=HUMAN))
    assert a.identity.workspace_id != b.identity.workspace_id
    # Neither can reach the other's workspace-visible documents.
    assert f"workspace:{a.identity.workspace_id}" not in b.identity.principals.values


async def test_signup_emails_a_verification_code(env: Env) -> None:
    await env.signup()
    (mail,) = env.mailer.sent
    assert mail.to == "ada@acme.dev"
    code = env.mailer.last_code()
    assert len(code) == 6
    assert code.isdigit()
    assert code in mail.text
    assert "http" not in mail.text  # a code, never a link


async def test_signup_validates_fields_before_spending_the_human_check(env: Env) -> None:
    with pytest.raises(ValidationFailed) as info:
        await env.signup(SignupRequest(" ", "not-an-email", "short", human_token=HUMAN))
    assert set(fields_of(info)) == {"name", "email", "password"}
    assert env.human.calls == 0
    assert env.accounts.users == {}


async def test_signup_rejects_disposable_inboxes(env: Env) -> None:
    with pytest.raises(ValidationFailed) as info:
        await env.signup(SignupRequest("Eve", "eve@x.mailinator.com", PASSWORD, human_token=HUMAN))
    assert "disposable" in fields_of(info)["email"]


@pytest.mark.parametrize(
    "variant", ["a.d.a+spam@gmail.com", "ADA@googlemail.com", "ada+2@gmail.com"]
)
async def test_one_inbox_cannot_hold_two_accounts(env: Env, variant: str) -> None:
    await env.signup(SignupRequest("Ada", "ada@gmail.com", PASSWORD, human_token=HUMAN))
    with pytest.raises(Conflict) as info:
        await env.signup(SignupRequest("Ada", variant, PASSWORD, human_token=HUMAN))
    assert "email" in fields_of(info)
    assert len(env.accounts.users) == 1


async def test_signup_variants_sign_in_to_the_same_account(env: Env) -> None:
    await env.signup(SignupRequest("Ada", "a.da@gmail.com", PASSWORD, human_token=HUMAN))
    assert await login(env, "ADA@googlemail.com")


async def test_signup_needs_a_human_check(env: Env) -> None:
    with pytest.raises(PermissionDenied):
        await env.signup(SignupRequest("Ada", "ada@acme.dev", PASSWORD, human_token=""))
    with pytest.raises(PermissionDenied):
        await env.signup(SignupRequest("Ada", "ada@acme.dev", PASSWORD, human_token="bot"))
    assert env.accounts.users == {}


async def test_signup_fails_closed_when_the_human_check_is_down(env: Env) -> None:
    env.human.down = True
    with pytest.raises(UpstreamUnavailable):
        await env.signup()
    assert env.accounts.users == {}


async def test_honeypot_is_refused_without_saying_why(env: Env) -> None:
    with pytest.raises(ValidationFailed) as info:
        await env.signup(
            SignupRequest("Bot", "bot@acme.dev", PASSWORD, human_token=HUMAN, trap="x")
        )
    assert "fields" not in info.value.extra
    assert env.accounts.users == {}


async def test_signups_are_limited_per_ip(env: Env) -> None:
    for n in range(SIGNUPS_PER_IP_PER_HOUR):
        await env.signup(SignupRequest("U", f"u{n}@acme.dev", PASSWORD, human_token=HUMAN))
    with pytest.raises(RateLimited):
        await env.signup(SignupRequest("U", "late@acme.dev", PASSWORD, human_token=HUMAN))
    # Another IP still gets in; and the first recovers after the hour.
    await env.signup(
        SignupRequest("U", "other@acme.dev", PASSWORD, human_token=HUMAN), ip="5.5.5.5"
    )
    env.clock.advance(timedelta(hours=1, seconds=1))
    await env.signup(SignupRequest("U", "later@acme.dev", PASSWORD, human_token=HUMAN))


async def test_signups_are_capped_globally_per_day(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    import core.services.auth as auth_module

    monkeypatch.setattr(auth_module, "SIGNUPS_PER_DAY", 2)
    for n in range(2):
        await env.signup(
            SignupRequest("U", f"u{n}@acme.dev", PASSWORD, human_token=HUMAN), ip=f"9.0.0.{n}"
        )
    with pytest.raises(RateLimited):
        await env.signup(
            SignupRequest("U", "x@acme.dev", PASSWORD, human_token=HUMAN), ip="9.0.0.9"
        )


async def test_kill_switch_closes_signup_but_not_login() -> None:
    env = Env()
    await env.signup()
    closed = Env(signups_enabled=False)
    closed.accounts, closed.auth = env.accounts, AuthService(
        accounts=env.accounts, sessions=env.sessions, throttle=env.throttle, hasher=env.hasher,
        codes=env.codes, rate=env.rate, human=env.human, mailer=env.mailer, clock=env.clock,
        policy=AuthPolicy(code_key=b"k" * 32, signups_enabled=False), events=env.events,
    )  # fmt: skip
    with pytest.raises(PermissionDenied, match="closed"):
        await closed.signup(SignupRequest("B", "b@acme.dev", PASSWORD, human_token=HUMAN))
    assert await closed.auth.login("ada@acme.dev", PASSWORD, ip="1.2.3.4", user_agent=None)


async def test_signup_survives_a_mail_outage(env: Env) -> None:
    env.mailer.down = True
    issued = await env.signup()
    assert issued.identity.user_id in env.accounts.users  # resend works once mail is back
    env.mailer.down = False
    await env.auth.resend_verification(issued.identity, human_token=HUMAN, ip="1.2.3.4")
    assert len(env.mailer.sent) == 1


async def test_password_is_stored_hashed(env: Env) -> None:
    await env.signup()
    user = await env.accounts.find_user("ada@acme.dev")
    assert user is not None
    assert user.password_hash != PASSWORD
    assert env.hasher.verify(user.password_hash, PASSWORD)


# ---------------------------------------------------------------- owner claim


async def test_owner_email_needs_the_setup_code_when_one_is_configured() -> None:
    env = Env(owner_email="ada@acme.dev", setup_token="s3tup-code")
    with pytest.raises(ValidationFailed) as info:
        await env.signup()
    assert "setup_token" in fields_of(info)
    with pytest.raises(ValidationFailed, match="highlighted"):
        await env.signup(
            SignupRequest("Ada", "ada@acme.dev", PASSWORD, human_token=HUMAN, setup_token="nope")
        )
    ok = SignupRequest(
        "Ada", "ada+x@acme.dev", PASSWORD, human_token=HUMAN, setup_token="s3tup-code"
    )
    assert await env.signup(ok)  # +tag variant is still the owner's inbox


async def test_owner_email_is_reserved_in_prod_without_a_setup_code() -> None:
    env = Env(owner_email="ada@acme.dev", setup_token_required=True)
    with pytest.raises(ValidationFailed) as info:
        await env.signup()
    assert "reserved" in fields_of(info)["email"]
    # Everyone else can still sign up.
    assert await env.signup(SignupRequest("Bob", "bob@acme.dev", PASSWORD, human_token=HUMAN))


async def test_setup_code_guesses_are_rate_limited() -> None:
    env = Env(owner_email="ada@acme.dev", setup_token="s3tup-code")
    for _ in range(SIGNUPS_PER_IP_PER_HOUR):
        with pytest.raises(ValidationFailed):
            await env.signup(
                SignupRequest(
                    "Ada", "ada@acme.dev", PASSWORD, human_token=HUMAN, setup_token="guess"
                )
            )
    with pytest.raises(RateLimited):
        await env.signup(
            SignupRequest(
                "Ada", "ada@acme.dev", PASSWORD, human_token=HUMAN, setup_token="s3tup-code"
            )
        )


async def test_platform_admin_only_once_the_owner_email_is_verified() -> None:
    env = Env(owner_email="ada@acme.dev")
    issued = await env.signup()
    assert not issued.identity.is_platform_admin
    me = await env.auth.verify_email(issued.identity, env.mailer.last_code(), ip=None)
    assert me.is_platform_admin
    assert (await env.auth.authenticate(await login(env))).is_platform_admin  # type: ignore[union-attr]
    other = await env.verified(SignupRequest("Bob", "bob@acme.dev", PASSWORD, human_token=HUMAN))
    assert not other.is_platform_admin


# ---------------------------------------------------------------- verification


async def test_the_emailed_code_verifies_the_account(env: Env) -> None:
    issued = await env.signup()
    me = await env.auth.verify_email(issued.identity, f" {env.mailer.last_code()} ", ip=None)
    assert me.email_verified
    again = await env.auth.authenticate(issued.session_token)
    assert again is not None
    assert again.email_verified


async def test_a_code_works_only_once(env: Env) -> None:
    issued = await env.signup()
    code = env.mailer.last_code()
    await env.auth.verify_email(issued.identity, code, ip=None)
    user = issued.identity.user_id
    assert await env.codes.check(user, CodePurpose.VERIFY_EMAIL, b"", max_attempts=5) == "expired"


async def test_wrong_codes_lock_the_code_after_the_limit(env: Env) -> None:
    issued = await env.signup()
    right = env.mailer.last_code()
    wrong = "000000" if right != "000000" else "111111"
    for _ in range(MAX_CODE_ATTEMPTS):
        with pytest.raises(ValidationFailed, match="isn't right"):
            await env.auth.verify_email(issued.identity, wrong, ip=None)
    with pytest.raises(ValidationFailed, match="Too many"):
        await env.auth.verify_email(issued.identity, right, ip=None)  # even the right one


async def test_malformed_codes_do_not_use_up_attempts(env: Env) -> None:
    issued = await env.signup()
    for _ in range(MAX_CODE_ATTEMPTS + 2):
        with pytest.raises(ValidationFailed):
            await env.auth.verify_email(issued.identity, "abc", ip=None)
    assert (
        await env.auth.verify_email(issued.identity, env.mailer.last_code(), ip=None)
    ).email_verified


async def test_codes_expire(env: Env) -> None:
    issued = await env.signup()
    env.clock.advance(CODE_TTL + timedelta(seconds=1))
    with pytest.raises(ValidationFailed, match="expired"):
        await env.auth.verify_email(issued.identity, env.mailer.last_code(), ip=None)


async def test_resend_invalidates_the_previous_code(env: Env) -> None:
    issued = await env.signup()
    first = env.mailer.last_code()
    await env.auth.resend_verification(issued.identity, human_token=HUMAN, ip=None)
    second = env.mailer.last_code()
    if first != second:  # 1-in-a-million they collide
        with pytest.raises(ValidationFailed):
            await env.auth.verify_email(issued.identity, first, ip=None)
    assert (await env.auth.verify_email(issued.identity, second, ip=None)).email_verified


async def test_resend_needs_a_human_and_is_limited(env: Env) -> None:
    issued = await env.signup()  # sent 1
    with pytest.raises(PermissionDenied):
        await env.auth.resend_verification(issued.identity, human_token="", ip=None)
    for _ in range(CODE_SENDS_PER_USER_PER_HOUR - 1):
        await env.auth.resend_verification(issued.identity, human_token=HUMAN, ip=None)
    with pytest.raises(RateLimited):
        await env.auth.resend_verification(issued.identity, human_token=HUMAN, ip=None)
    assert len(env.mailer.sent) == CODE_SENDS_PER_USER_PER_HOUR


async def test_code_checks_are_limited_per_ip(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    import core.services.auth as auth_module

    monkeypatch.setattr(auth_module, "CODE_CHECKS_PER_IP_PER_HOUR", 2)
    issued = await env.signup()
    for _ in range(2):
        with pytest.raises(ValidationFailed):
            await env.auth.verify_email(issued.identity, "123", ip="6.6.6.6")
    with pytest.raises(RateLimited):
        await env.auth.verify_email(issued.identity, env.mailer.last_code(), ip="6.6.6.6")


async def test_a_code_is_bound_to_its_account_and_purpose(env: Env) -> None:
    a = await env.signup()
    code_a = env.mailer.last_code()
    b = await env.signup(SignupRequest("Bob", "bob@acme.dev", PASSWORD, human_token=HUMAN))
    if code_a != env.mailer.last_code():
        with pytest.raises(ValidationFailed):
            await env.auth.verify_email(b.identity, code_a, ip=None)
    # A verification code can't reset a password.
    with pytest.raises(ValidationFailed, match="isn't right or has expired"):
        await env.auth.reset_password("ada@acme.dev", code_a, "a brand new password", ip=None)
    assert a.identity.user_id != b.identity.user_id


async def test_unverified_accounts_are_swept_after_the_ttl(env: Env) -> None:
    await env.signup()
    await env.verified(SignupRequest("Bob", "bob@acme.dev", PASSWORD, human_token=HUMAN))
    assert await env.auth.sweep_unverified() == 0
    env.clock.advance(UNVERIFIED_ACCOUNT_TTL + timedelta(seconds=1))
    assert await env.auth.sweep_unverified() == 1
    assert await env.accounts.find_user("ada@acme.dev") is None
    assert await env.accounts.find_user("bob@acme.dev") is not None
    assert len(env.accounts.workspaces) == 1  # Ada's workspace went with her


# ---------------------------------------------------------------- password reset


async def test_password_reset_sets_a_new_password_and_signs_out_everywhere(env: Env) -> None:
    await env.signup()
    old = await login(env)
    await env.auth.request_password_reset("ADA@acme.dev", human_token=HUMAN, ip=None)
    mail = env.mailer.sent[-1]
    assert "reset" in mail.subject
    await env.auth.reset_password(
        "ada@acme.dev", env.mailer.last_code(), "a brand new password", ip=None
    )
    assert await env.auth.authenticate(old) is None
    with pytest.raises(Unauthenticated):
        await login(env)
    me = await env.auth.authenticate(await login(env, password="a brand new password"))
    assert me is not None
    assert me.email_verified


async def test_reset_request_answers_the_same_for_unknown_emails(env: Env) -> None:
    await env.auth.request_password_reset("nobody@acme.dev", human_token=HUMAN, ip=None)  # no error
    assert env.mailer.sent == []


async def test_reset_request_needs_a_human(env: Env) -> None:
    with pytest.raises(PermissionDenied):
        await env.auth.request_password_reset("ada@acme.dev", human_token="", ip=None)


async def test_reset_with_a_bad_code_or_unknown_email_shares_one_message(env: Env) -> None:
    await env.signup()
    await env.auth.request_password_reset("ada@acme.dev", human_token=HUMAN, ip=None)
    for email in ("ada@acme.dev", "nobody@acme.dev"):
        with pytest.raises(ValidationFailed, match=BAD_RESET_CODE):
            await env.auth.reset_password(
                email,
                "999999" if env.mailer.last_code() != "999999" else "888888",
                "a brand new password",
                ip=None,
            )


async def test_reset_validates_the_new_password(env: Env) -> None:
    await env.signup()
    with pytest.raises(ValidationFailed) as info:
        await env.auth.reset_password("ada@acme.dev", "123456", "short", ip=None)
    assert "password" in fields_of(info)


# ---------------------------------------------------------------- login


async def test_login_issues_a_fresh_session_each_time(env: Env) -> None:
    await env.signup()
    a, b = await login(env), await login(env)
    assert a != b
    assert await env.auth.authenticate(a) is not None
    assert await env.auth.authenticate(b) is not None


@pytest.mark.parametrize(
    ("email", "password"),
    [
        ("ada@acme.dev", "wrong password!"),
        ("nobody@acme.dev", PASSWORD),
        ("not-an-email", PASSWORD),
    ],
    ids=["wrong-password", "unknown-email", "malformed-email"],
)
async def test_login_failures_share_one_generic_message(
    env: Env, email: str, password: str
) -> None:
    await env.signup()
    with pytest.raises(Unauthenticated, match=GENERIC_LOGIN_ERROR):
        await login(env, email, password)


async def test_unknown_email_still_costs_a_password_verification(env: Env) -> None:
    calls: list[str] = []
    original = env.hasher.verify

    def counting(password_hash: str, password: str) -> bool:
        calls.append(password_hash)
        return original(password_hash, password)

    env.hasher.verify = counting
    with pytest.raises(Unauthenticated):
        await login(env, "nobody@acme.dev", PASSWORD)
    assert len(calls) == 1  # same work as a real account: no timing oracle


async def test_disabled_users_cannot_log_in(env: Env) -> None:
    issued = await env.signup()
    env.accounts.disable(issued.identity.user_id)
    with pytest.raises(Unauthenticated):
        await login(env)


async def test_unverified_users_can_log_in_but_are_marked_unverified(env: Env) -> None:
    await env.signup()
    me = await env.auth.authenticate(await login(env))
    assert me is not None
    assert not me.email_verified


# ---------------------------------------------------------------- throttling


async def test_repeated_failures_from_one_ip_are_throttled_then_recover(env: Env) -> None:
    await env.signup()
    for _ in range(MAX_FAILURES_PER_EMAIL_IP):
        await fail(env)
    with pytest.raises(RateLimited) as info:
        await login(env)  # even the right password is refused while throttled
    assert info.value.retry_after_s == int(LOGIN_WINDOW.total_seconds())
    env.clock.advance(LOGIN_WINDOW + timedelta(seconds=1))
    assert await login(env)


async def test_a_stranger_cannot_lock_the_owner_out(env: Env) -> None:
    await env.signup()
    for _ in range(MAX_FAILURES_PER_EMAIL_IP):
        await fail(env, ip="6.6.6.6")
    assert await login(env, ip="1.2.3.4")  # the owner, from their own IP, still gets in


async def test_one_ip_spraying_many_emails_is_throttled(env: Env) -> None:
    await env.signup()
    for n in range(MAX_FAILURES_PER_IP):
        await fail(env, ip="6.6.6.6", email=f"victim{n}@acme.dev")
    with pytest.raises(RateLimited):
        await login(env, ip="6.6.6.6")


async def test_unknown_ip_shares_one_bucket_instead_of_being_unlimited(env: Env) -> None:
    await env.signup()
    for _ in range(MAX_FAILURES_PER_EMAIL_IP):
        await fail(env, ip=None)
    with pytest.raises(RateLimited):
        await login(env, ip=None)
    assert any(a[1] == UNKNOWN_IP for a in env.throttle.attempts)


async def test_parallel_guesses_cannot_exceed_the_limit(env: Env) -> None:
    await env.signup()

    async def guess() -> str:
        try:
            await login(env, password="wrong password!")
        except RateLimited:
            return "limited"
        except Unauthenticated:
            return "checked"
        return "in"

    results = await asyncio.gather(*(guess() for _ in range(30)))
    assert results.count("checked") == MAX_FAILURES_PER_EMAIL_IP  # attempts recorded up front


# ---------------------------------------------------------------- sessions


async def test_login_returns_the_identity_without_reading_the_session_back(env: Env) -> None:
    await env.signup()
    gets = env.sessions.gets
    issued = await env.auth.login("ada@acme.dev", PASSWORD, ip="1.2.3.4", user_agent="t")
    assert env.sessions.gets == gets  # create resolves the session in the same round trip
    assert issued.identity == await env.auth.authenticate(issued.session_token)


async def test_login_is_rejected_if_the_user_cannot_hold_a_session(env: Env) -> None:
    issued = await env.signup()
    del env.accounts.members[issued.identity.user_id]  # e.g. membership removed mid-login
    with pytest.raises(Unauthenticated, match=GENERIC_LOGIN_ERROR):
        await login(env)


async def test_unknown_expired_and_logged_out_tokens_are_rejected(env: Env) -> None:
    await env.signup()
    assert await env.auth.authenticate(None) is None
    assert await env.auth.authenticate("forged") is None
    token = await login(env)
    identity = await env.auth.authenticate(token)
    assert identity is not None
    await env.auth.logout(identity)
    assert await env.auth.authenticate(token) is None
    token = await login(env)
    env.clock.advance(SESSION_TTL + timedelta(seconds=1))
    assert await env.auth.authenticate(token) is None


async def test_sessions_slide_but_only_write_every_few_minutes(env: Env) -> None:
    issued = await env.signup()
    await env.auth.authenticate(issued.session_token)
    assert env.sessions.touches == 0
    env.clock.advance(SESSION_TOUCH_EVERY)
    await env.auth.authenticate(issued.session_token)
    assert env.sessions.touches == 1


async def test_only_the_token_hash_is_stored(env: Env) -> None:
    token = (await env.signup()).session_token
    stored = [row["token_hash"] for row in env.sessions.rows.values()]
    assert hash_token(token) in stored
    assert all(token.encode() != h for h in stored)


# ------------------------------------------------------- auth events (ADR-0042)


async def test_auth_flow_records_events_without_personal_data(env: Env) -> None:
    issued = await env.signup()
    await env.auth.verify_email(issued.identity, env.mailer.last_code(), ip="1.2.3.4")
    with pytest.raises(Unauthenticated):
        await env.auth.login("ada@acme.dev", "wrong password!!", ip="1.2.3.4", user_agent=None)
    with pytest.raises(Unauthenticated):
        await env.auth.login("nobody@acme.dev", PASSWORD, ip="1.2.3.4", user_agent=None)
    session = await env.auth.login("ada@acme.dev", PASSWORD, ip="1.2.3.4", user_agent=None)
    await env.auth.logout(session.identity)
    uid = issued.identity.user_id
    assert env.events.rows
    assert [(k, u) for k, u, _, _ in env.events.rows] == [
        (K.SIGNUP, uid),
        (K.EMAIL_VERIFIED, uid),
        (K.LOGIN_FAILED, uid),
        (K.LOGIN_FAILED, None),
        (K.LOGIN_SUCCESS, uid),
        (K.LOGOUT, uid),
    ]
    # The port takes only kind, user and actor: no IP, email or user agent can be stored.
    assert all(len(r) == 4 for r in env.events.rows)


async def test_throttled_login_is_recorded(env: Env) -> None:
    await env.signup()
    for _ in range(MAX_FAILURES_PER_EMAIL_IP):
        with pytest.raises(Unauthenticated):
            await env.auth.login("ada@acme.dev", "nope nope nope", ip="9.9.9.9", user_agent=None)
    with pytest.raises(RateLimited):
        await env.auth.login("ada@acme.dev", PASSWORD, ip="9.9.9.9", user_agent=None)
    assert env.events.kinds()[-1] is K.LOGIN_THROTTLED


async def test_event_log_outage_never_blocks_sign_in(env: Env) -> None:
    await env.signup()
    env.events.down = True
    assert await env.auth.login("ada@acme.dev", PASSWORD, ip="1.2.3.4", user_agent=None)


async def test_outdated_hash_is_upgraded_on_sign_in(env: Env) -> None:
    issued = await env.signup()
    uid = issued.identity.user_id
    env.hasher.needs_rehash = lambda h: h.startswith("fake$")  # type: ignore[method-assign]
    env.hasher.hash = lambda p: f"new${p[::-1]}"  # type: ignore[method-assign]
    env.hasher.verify = lambda h, p: h.endswith(p[::-1])  # type: ignore[method-assign]
    await env.auth.login("ada@acme.dev", PASSWORD, ip="1.2.3.4", user_agent=None)
    assert env.accounts.users[uid].password_hash.startswith("new$")


async def test_rehash_never_undoes_a_concurrent_reset(env: Env) -> None:
    issued = await env.signup()
    uid = issued.identity.user_id
    env.hasher.needs_rehash = lambda h: True  # type: ignore[method-assign]
    real_hash = env.hasher.hash

    def hash_and_race(p: str) -> str:
        # A reset commits between sign-in's verify and its rehash write.
        env.accounts.users[uid] = replace(env.accounts.users[uid], password_hash="reset$new")
        return real_hash(p)

    env.hasher.hash = hash_and_race  # type: ignore[method-assign]
    await env.auth.login("ada@acme.dev", PASSWORD, ip="1.2.3.4", user_agent=None)
    assert env.accounts.users[uid].password_hash == "reset$new"


async def test_repeated_throttled_logins_store_one_event(env: Env) -> None:
    await env.signup()
    for _ in range(MAX_FAILURES_PER_EMAIL_IP):
        with pytest.raises(Unauthenticated):
            await env.auth.login("ada@acme.dev", "nope nope nope", ip="9.9.9.9", user_agent=None)
    for _ in range(20):
        with pytest.raises(RateLimited):
            await env.auth.login("ada@acme.dev", PASSWORD, ip="9.9.9.9", user_agent=None)
    assert env.events.kinds().count(K.LOGIN_THROTTLED) == 1
