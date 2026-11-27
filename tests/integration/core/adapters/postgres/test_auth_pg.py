"""Postgres auth stores against a real database (ADR-0021).

No longer destructive: sign-up isn't global any more, so every test uses its own random
emails and deletes only the accounts it created. Rate limits run in memory here (the
Postgres limiter has its own test), so CI runs never use up the branch's daily caps.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text

from core.adapters.postgres.admin import PostgresAuthEventLog
from core.adapters.postgres.auth import (
    PostgresAccountStore,
    PostgresCodeStore,
    PostgresLoginThrottle,
    PostgresSessionStore,
)
from core.adapters.postgres.uow import UnitOfWork
from core.adapters.security.argon2_hasher import Argon2Hasher
from core.adapters.system import SystemClock
from core.domain.auth import CodeCheck, CodePurpose, Role, hash_code
from core.domain.errors import Conflict
from core.domain.ids import UserId
from core.ports.auth import ThrottleLimits
from core.services.auth import AuthPolicy, AuthService, SignupRequest
from tests.fakes.auth import FakeHumanVerifier, FakeMailer
from tests.fakes.rate_limit import InMemoryRateLimiter
from tests.fakes.system import FakeClock

PASSWORD = "correct horse battery"
KEY = b"k" * 32


class Run:
    """A test's own email namespace, cleaned up afterwards."""

    def __init__(self) -> None:
        self.tag = uuid4().hex[:10]

    def email(self, who: str) -> str:
        return f"{who}-{self.tag}@acme.dev"


@pytest.fixture
async def run(uow: UnitOfWork) -> AsyncIterator[Run]:
    r = Run()
    yield r
    async with uow() as s:
        like = {"p": f"%-{r.tag}@acme.dev"}
        await s.execute(
            text(
                "DELETE FROM workspaces WHERE id IN (SELECT m.workspace_id FROM memberships m"
                " JOIN users u ON u.id = m.user_id WHERE u.email LIKE :p)"
            ),
            like,
        )
        await s.execute(text("DELETE FROM users WHERE email LIKE :p"), like)
        await s.execute(text("DELETE FROM login_attempts WHERE email LIKE :p"), like)


def service(uow: UnitOfWork, mailer: FakeMailer | None = None) -> AuthService:
    return AuthService(
        accounts=PostgresAccountStore(uow),
        sessions=PostgresSessionStore(uow),
        throttle=PostgresLoginThrottle(uow),
        hasher=Argon2Hasher(),
        codes=PostgresCodeStore(uow),
        rate=InMemoryRateLimiter(FakeClock()),
        human=FakeHumanVerifier(),
        mailer=mailer or FakeMailer(),
        clock=SystemClock(),
        policy=AuthPolicy(code_key=KEY),
        events=PostgresAuthEventLog(uow),
    )


def req(email: str, name: str = "Ada") -> SignupRequest:
    return SignupRequest(name, email, PASSWORD, human_token=FakeHumanVerifier.PASS)


async def test_full_flow_signup_verify_login_logout(uow: UnitOfWork, run: Run) -> None:
    mailer = FakeMailer()
    auth = service(uow, mailer)
    issued = await auth.signup(req(run.email("ada")), ip="10.0.0.1", user_agent="t")
    me = issued.identity
    assert me.role is Role.OWNER
    assert not me.email_verified
    # The INSERT ... RETURNING CTE resolves the same identity a later read does.
    assert await auth.authenticate(issued.session_token) == me

    verified = await auth.verify_email(me, mailer.last_code(), ip=None)
    assert verified.email_verified
    again = await auth.authenticate(issued.session_token)
    assert again is not None
    assert again.email_verified

    login = await auth.login(run.email("ada").upper(), PASSWORD, ip="10.0.0.1", user_agent=None)
    assert login.identity.workspace_id == me.workspace_id
    await auth.logout(login.identity)
    assert await auth.authenticate(login.session_token) is None


async def test_each_signup_gets_its_own_workspace(uow: UnitOfWork, run: Run) -> None:
    auth = service(uow)
    a = (await auth.signup(req(run.email("a")), ip=None, user_agent=None)).identity
    b = (await auth.signup(req(run.email("b"), "Bob"), ip=None, user_agent=None)).identity
    assert a.workspace_id != b.workspace_id
    async with uow() as s:
        name = (
            await s.execute(
                text("SELECT name FROM workspaces WHERE id = :w"), {"w": a.workspace_id}
            )
        ).scalar_one()
    assert name == "Ada's workspace"


async def test_concurrent_signups_with_one_inbox_only_one_wins(uow: UnitOfWork, run: Run) -> None:
    store = PostgresAccountStore(uow)
    canonical = run.email("race")

    async def attempt(n: int) -> object:
        try:
            return await store.create_user_with_workspace(
                email=f"{canonical.split('@')[0]}+{n}@acme.dev", email_canonical=canonical,
                name="r", password_hash="h", workspace_name="w", slug=f"race-{run.tag}-{n}",
            )  # fmt: skip
        except Conflict as e:
            return e

    results = await asyncio.gather(*(attempt(n) for n in range(4)))
    assert sum(not isinstance(r, Conflict) for r in results) == 1
    async with uow() as s:
        orphans = (
            await s.execute(
                text("SELECT count(*) FROM workspaces WHERE slug LIKE :s"),
                {"s": f"race-{run.tag}-%"},
            )
        ).scalar_one()
    assert orphans == 1  # the losers created no workspace either


async def test_code_store_is_single_use_and_counts_attempts(uow: UnitOfWork, run: Run) -> None:
    auth = service(uow)
    me = (await auth.signup(req(run.email("c")), ip=None, user_agent=None)).identity
    codes, user, purpose = PostgresCodeStore(uow), me.user_id, CodePurpose.RESET_PASSWORD
    right = hash_code(KEY, user, purpose, "123456")
    wrong = hash_code(KEY, user, purpose, "654321")
    await codes.replace(user, purpose, right, timedelta(minutes=15))
    assert await codes.check(user, purpose, wrong, max_attempts=2) is CodeCheck.WRONG
    assert await codes.check(user, purpose, right, max_attempts=2) is CodeCheck.OK
    assert await codes.check(user, purpose, right, max_attempts=2) is CodeCheck.EXPIRED  # used

    await codes.replace(user, purpose, right, timedelta(minutes=15))  # a fresh code resets
    results = await asyncio.gather(
        *(codes.check(user, purpose, wrong, max_attempts=2) for _ in range(5))
    )
    assert results.count(CodeCheck.WRONG) == 2  # the row lock serialises the guesses
    assert await codes.check(user, purpose, right, max_attempts=2) is CodeCheck.LOCKED

    await codes.replace(user, purpose, right, timedelta(seconds=-1))
    assert await codes.check(user, purpose, right, max_attempts=2) is CodeCheck.EXPIRED


async def test_password_reset_replaces_the_hash_and_ends_sessions(
    uow: UnitOfWork, run: Run
) -> None:
    mailer = FakeMailer()
    auth = service(uow, mailer)
    token = (await auth.signup(req(run.email("r")), ip=None, user_agent=None)).session_token
    await auth.request_password_reset(run.email("r"), human_token=FakeHumanVerifier.PASS, ip=None)
    await auth.reset_password(run.email("r"), mailer.last_code(), "a brand new password", ip=None)
    assert await auth.authenticate(token) is None
    assert await auth.login(run.email("r"), "a brand new password", ip=None, user_agent=None)


async def test_sweep_deletes_stale_unverified_accounts_and_their_workspaces(
    uow: UnitOfWork, run: Run
) -> None:
    mailer = FakeMailer()
    auth = service(uow, mailer)
    stale = (await auth.signup(req(run.email("stale")), ip=None, user_agent=None)).identity
    kept = await auth.signup(req(run.email("kept"), "Kim"), ip=None, user_agent=None)
    await auth.verify_email(kept.identity, mailer.last_code(), ip=None)
    async with uow() as s:
        await s.execute(
            text("UPDATE users SET created_at = now() - interval '3 days' WHERE email LIKE :p"),
            {"p": f"%-{run.tag}@acme.dev"},
        )
    store = PostgresAccountStore(uow)
    assert await store.delete_unverified_before(datetime.now(UTC) - timedelta(hours=48)) >= 1
    assert await store.find_user(run.email("stale")) is None
    assert await store.find_user(run.email("kept")) is not None
    async with uow() as s:
        gone = (
            await s.execute(
                text("SELECT count(*) FROM workspaces WHERE id = :w"), {"w": stale.workspace_id}
            )
        ).scalar_one()
    assert gone == 0


async def test_throttle_reserves_attempts_atomically(uow: UnitOfWork, run: Run) -> None:
    throttle = PostgresLoginThrottle(uow)
    email, ip = run.email("x"), f"10.{uuid4().int % 250}.{uuid4().int % 250}.9"
    limits = ThrottleLimits(window=timedelta(minutes=15), per_email_ip=3, per_ip=20, per_email=100)
    results = await asyncio.gather(
        *(throttle.begin_attempt(email=email, ip=ip, limits=limits) for _ in range(8))
    )
    assert sum(r is not None for r in results) == 3  # the advisory lock serialises the check
    granted = next(r for r in results if r is not None)
    await throttle.mark_success(granted)  # successes stop counting
    assert await throttle.begin_attempt(email=email, ip=ip, limits=limits)
    # A different IP for the same email isn't blocked by this IP's failures.
    assert await throttle.begin_attempt(email=email, ip="10.255.255.1", limits=limits)


async def test_session_groups_are_limited_to_the_users_workspace(uow: UnitOfWork, run: Run) -> None:
    auth = service(uow)
    issued = await auth.signup(req(run.email("g")), ip=None, user_agent=None)
    async with uow() as s:
        other_ws = (
            await s.execute(
                text("INSERT INTO workspaces (name, slug) VALUES ('o', :s) RETURNING id"),
                {"s": f"o-{run.tag}"},
            )
        ).scalar_one()
        foreign = (
            await s.execute(
                text("INSERT INTO groups (workspace_id, name) VALUES (:w, 'g') RETURNING id"),
                {"w": other_ws},
            )
        ).scalar_one()
        await s.execute(
            text("INSERT INTO group_members (group_id, user_id) VALUES (:g, :u)"),
            {"g": foreign, "u": issued.identity.user_id},
        )
    identity = await auth.authenticate(issued.session_token)
    assert identity is not None
    assert f"group:{foreign}" not in identity.principals.values
    async with uow() as s:
        await s.execute(text("DELETE FROM workspaces WHERE id = :w"), {"w": other_ws})


async def test_expired_session_is_not_returned(uow: UnitOfWork, run: Run) -> None:
    auth = service(uow)
    issued = await auth.signup(req(run.email("e")), ip=None, user_agent=None)
    async with uow() as s:
        await s.execute(
            text("UPDATE sessions SET expires_at = now() - interval '1 second' WHERE user_id = :u"),
            {"u": UserId(issued.identity.user_id)},
        )
    assert await auth.authenticate(issued.session_token) is None
