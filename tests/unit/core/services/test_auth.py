from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from core.domain.auth import (
    LOGIN_WINDOW,
    MAX_ACTIVE_GUEST_SESSIONS,
    MAX_FAILURES_PER_EMAIL_IP,
    MAX_FAILURES_PER_IP,
    SESSION_TOUCH_EVERY,
    SESSION_TTL,
    UNKNOWN_IP,
    Role,
    hash_token,
)
from core.domain.errors import Conflict, RateLimited, Unauthenticated, ValidationFailed
from core.domain.principals import PUBLIC
from core.services.auth import GENERIC_LOGIN_ERROR, AuthService, SetupRequest
from tests.fakes.auth import FakeHasher, InMemoryAccounts, InMemorySessions, InMemoryThrottle
from tests.fakes.system import FakeClock

PASSWORD = "correct horse battery"
SETUP = SetupRequest("Acme", "Ada", "Ada@Acme.dev ", PASSWORD)


class Env:
    def __init__(
        self, *, setup_token: str | None = None, setup_token_required: bool = False
    ) -> None:
        self.clock = FakeClock()
        self.accounts = InMemoryAccounts()
        self.sessions = InMemorySessions(self.accounts, self.clock)
        self.throttle = InMemoryThrottle(self.clock)
        self.hasher = FakeHasher()
        self.auth = AuthService(
            self.accounts,
            self.sessions,
            self.throttle,
            self.hasher,
            self.clock,
            setup_token=setup_token,
            setup_token_required=setup_token_required,
        )


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


# ---------------------------------------------------------------- setup


async def test_setup_creates_owner_and_guest_and_signs_in(env: Env) -> None:
    assert not await env.auth.is_set_up()
    issued = await env.auth.setup(SETUP, ip=None, user_agent=None)
    identity = await env.auth.authenticate(issued.session_token)
    assert identity is not None
    assert identity.role is Role.OWNER
    assert identity.email == "ada@acme.dev"  # normalised
    assert await env.auth.is_set_up()
    assert await env.accounts.find_guest() is not None


async def test_setup_only_works_once(env: Env) -> None:
    await env.auth.setup(SETUP, ip=None, user_agent=None)
    with pytest.raises(Conflict):
        await env.auth.setup(SETUP, ip=None, user_agent=None)


async def test_setup_validates_every_field(env: Env) -> None:
    with pytest.raises(ValidationFailed) as info:
        await env.auth.setup(
            SetupRequest(" ", " ", "not-an-email", "short"), ip=None, user_agent=None
        )
    fields = info.value.extra["fields"]
    assert isinstance(fields, dict)
    assert set(fields) == {"workspace_name", "name", "email", "password"}
    assert not await env.auth.is_set_up()


async def test_setup_requires_the_configured_setup_code() -> None:
    env = Env(setup_token="s3tup-code")
    assert env.auth.setup_requires_token
    with pytest.raises(ValidationFailed) as info:
        await env.auth.setup(SETUP, ip=None, user_agent=None)
    fields = info.value.extra["fields"]
    assert isinstance(fields, dict)
    assert "setup_token" in fields
    ok = SetupRequest("Acme", "Ada", "ada@acme.dev", PASSWORD, setup_token="s3tup-code")
    assert await env.auth.setup(ok, ip=None, user_agent=None)


async def test_setup_is_disabled_in_prod_without_a_setup_code() -> None:
    env = Env(setup_token_required=True)
    with pytest.raises(Conflict, match="SETUP_TOKEN"):
        await env.auth.setup(SETUP, ip=None, user_agent=None)
    assert not await env.auth.is_set_up()


async def test_password_is_stored_hashed(env: Env) -> None:
    await env.auth.setup(SETUP, ip=None, user_agent=None)
    user = await env.accounts.find_user_by_email("ada@acme.dev")
    assert user is not None
    assert user.password_hash != PASSWORD
    assert env.hasher.verify(user.password_hash or "", PASSWORD)


# ---------------------------------------------------------------- login


async def test_login_issues_a_fresh_session_each_time(env: Env) -> None:
    await env.auth.setup(SETUP, ip=None, user_agent=None)
    a, b = await login(env), await login(env)
    assert a != b
    assert await env.auth.authenticate(a) is not None
    assert await env.auth.authenticate(b) is not None


@pytest.mark.parametrize(
    ("email", "password"),
    [("ada@acme.dev", "wrong password!"), ("nobody@acme.dev", PASSWORD)],
    ids=["wrong-password", "unknown-email"],
)
async def test_login_failures_share_one_generic_message(
    env: Env, email: str, password: str
) -> None:
    await env.auth.setup(SETUP, ip=None, user_agent=None)
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


async def test_disabled_users_and_guest_cannot_password_login(env: Env) -> None:
    await env.auth.setup(SETUP, ip=None, user_agent=None)
    owner = await env.accounts.find_user_by_email("ada@acme.dev")
    assert owner is not None
    env.accounts.disable(owner.id)
    with pytest.raises(Unauthenticated):
        await login(env)
    with pytest.raises(Unauthenticated):
        await login(env, "guest@slate.invalid", "")


# ---------------------------------------------------------------- throttling


async def test_repeated_failures_from_one_ip_are_throttled_then_recover(env: Env) -> None:
    await env.auth.setup(SETUP, ip=None, user_agent=None)
    for _ in range(MAX_FAILURES_PER_EMAIL_IP):
        await fail(env)
    with pytest.raises(RateLimited) as info:
        await login(env)  # even the right password is refused while throttled
    assert info.value.retry_after_s == int(LOGIN_WINDOW.total_seconds())
    env.clock.advance(LOGIN_WINDOW + timedelta(seconds=1))
    assert await login(env)


async def test_a_stranger_cannot_lock_the_owner_out(env: Env) -> None:
    await env.auth.setup(SETUP, ip=None, user_agent=None)
    for _ in range(MAX_FAILURES_PER_EMAIL_IP):
        await fail(env, ip="6.6.6.6")
    assert await login(env, ip="1.2.3.4")  # the owner, from their own IP, still gets in


async def test_one_ip_spraying_many_emails_is_throttled(env: Env) -> None:
    await env.auth.setup(SETUP, ip=None, user_agent=None)
    for n in range(MAX_FAILURES_PER_IP):
        await fail(env, ip="6.6.6.6", email=f"victim{n}@acme.dev")
    with pytest.raises(RateLimited):
        await login(env, ip="6.6.6.6")


async def test_unknown_ip_shares_one_bucket_instead_of_being_unlimited(env: Env) -> None:
    await env.auth.setup(SETUP, ip=None, user_agent=None)
    for _ in range(MAX_FAILURES_PER_EMAIL_IP):
        await fail(env, ip=None)
    with pytest.raises(RateLimited):
        await login(env, ip=None)
    assert any(a[1] == UNKNOWN_IP for a in env.throttle.attempts)


async def test_parallel_guesses_cannot_exceed_the_limit(env: Env) -> None:
    await env.auth.setup(SETUP, ip=None, user_agent=None)

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


# ---------------------------------------------------------------- guests


async def test_guest_gets_only_public_and_self(env: Env) -> None:
    await env.auth.setup(SETUP, ip=None, user_agent=None)
    issued = await env.auth.login_as_guest(ip="9.9.9.9", user_agent=None)
    guest = await env.auth.authenticate(issued.session_token)
    assert guest is not None
    assert guest.is_guest
    assert set(guest.principals.values) == {f"user:{guest.user_id}", PUBLIC}
    assert not guest.can(Role.VIEWER)  # guests can't perform any mutating action


async def test_guest_unavailable_before_setup(env: Env) -> None:
    with pytest.raises(Conflict):
        await env.auth.login_as_guest(ip=None, user_agent=None)


async def test_guest_sessions_are_throttled_per_ip(env: Env) -> None:
    await env.auth.setup(SETUP, ip=None, user_agent=None)
    for _ in range(MAX_FAILURES_PER_IP):
        await env.auth.login_as_guest(ip="7.7.7.7", user_agent=None)
    with pytest.raises(RateLimited):
        await env.auth.login_as_guest(ip="7.7.7.7", user_agent=None)


async def test_guest_sessions_are_capped_globally(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    import core.services.auth as auth_module

    monkeypatch.setattr(auth_module, "MAX_ACTIVE_GUEST_SESSIONS", 2)
    await env.auth.setup(SETUP, ip=None, user_agent=None)
    for n in range(2):
        await env.auth.login_as_guest(ip=f"8.8.8.{n}", user_agent=None)
    with pytest.raises(RateLimited, match="busy"):
        await env.auth.login_as_guest(ip="8.8.8.9", user_agent=None)
    assert MAX_ACTIVE_GUEST_SESSIONS > 2  # the real cap is larger


# ---------------------------------------------------------------- sessions


async def test_authenticate_resolves_principals(env: Env) -> None:
    await env.auth.setup(SETUP, ip=None, user_agent=None)
    identity = await env.auth.authenticate(await login(env))
    assert identity is not None
    assert f"user:{identity.user_id}" in identity.principals.values
    assert f"workspace:{identity.workspace_id}" in identity.principals.values


async def test_unknown_expired_and_logged_out_tokens_are_rejected(env: Env) -> None:
    await env.auth.setup(SETUP, ip=None, user_agent=None)
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
    await env.auth.setup(SETUP, ip=None, user_agent=None)
    token = await login(env)
    await env.auth.authenticate(token)
    assert env.sessions.touches == 0
    env.clock.advance(SESSION_TOUCH_EVERY)
    await env.auth.authenticate(token)
    assert env.sessions.touches == 1


async def test_only_the_token_hash_is_stored(env: Env) -> None:
    await env.auth.setup(SETUP, ip=None, user_agent=None)
    token = await login(env)
    stored = [row["token_hash"] for row in env.sessions.rows.values()]
    assert hash_token(token) in stored
    assert all(token.encode() != h for h in stored)
