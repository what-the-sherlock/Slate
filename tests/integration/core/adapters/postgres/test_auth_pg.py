"""Postgres auth stores. DESTRUCTIVE: first-run setup is global ("does any owner
exist?"), so these tests clear all accounts on the branch before and after.
Opt in with SLATE_ALLOW_DESTRUCTIVE_TESTS=1 (CI's dedicated test branch does).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from sqlalchemy import text

from core.adapters.postgres.auth import (
    GUEST_EMAIL,
    PostgresAccountStore,
    PostgresLoginThrottle,
    PostgresSessionStore,
)
from core.adapters.postgres.uow import UnitOfWork
from core.adapters.security.argon2_hasher import Argon2Hasher
from core.adapters.system import SystemClock
from core.domain.auth import Role
from core.domain.errors import Conflict
from core.ports.auth import ThrottleLimits
from core.services.auth import AuthService, SetupRequest

pytestmark = pytest.mark.skipif(
    os.environ.get("SLATE_ALLOW_DESTRUCTIVE_TESTS") != "1",
    reason="clears all accounts; set SLATE_ALLOW_DESTRUCTIVE_TESTS=1 on a disposable branch",
)


async def _wipe(uow: UnitOfWork) -> None:
    async with uow() as s:
        await s.execute(text("DELETE FROM workspaces"))
        await s.execute(text("DELETE FROM users"))
        await s.execute(text("DELETE FROM login_attempts"))


@pytest.fixture
async def clean(uow: UnitOfWork) -> AsyncIterator[None]:
    await _wipe(uow)
    yield
    await _wipe(uow)


def service(uow: UnitOfWork) -> AuthService:
    return AuthService(
        PostgresAccountStore(uow),
        PostgresSessionStore(uow),
        PostgresLoginThrottle(uow),
        Argon2Hasher(),
        SystemClock(),
    )


SETUP = SetupRequest("Acme", "Ada", "ada@acme.dev", "correct horse battery")


async def test_full_flow_setup_login_guest_logout(uow: UnitOfWork, clean: None) -> None:
    auth = service(uow)
    assert not await auth.is_set_up()
    owner = await auth.authenticate(
        (await auth.setup(SETUP, ip="10.0.0.1", user_agent="t")).session_token
    )
    assert owner is not None
    assert owner.role is Role.OWNER

    token = (
        await auth.login("ADA@acme.dev", SETUP.password, ip="10.0.0.1", user_agent=None)
    ).session_token
    again = await auth.authenticate(token)
    assert again is not None
    assert again.workspace_id == owner.workspace_id

    guest = await auth.authenticate(
        (await auth.login_as_guest(ip="10.0.0.2", user_agent=None)).session_token
    )
    assert guest is not None
    assert guest.is_guest
    assert guest.role is Role.VIEWER
    assert f"workspace:{guest.workspace_id}" not in guest.principals.values

    await auth.logout(again)
    assert await auth.authenticate(token) is None


async def test_concurrent_setups_only_one_wins(uow: UnitOfWork, clean: None) -> None:
    store = PostgresAccountStore(uow)

    async def attempt(n: int) -> object:
        try:
            return await store.create_workspace_with_owner(
                workspace_name=f"W{n}", slug=f"w{n}", email=f"o{n}@acme.dev", name="o",
                password_hash="h",
            )  # fmt: skip
        except Conflict as e:
            return e

    results = await asyncio.gather(*(attempt(n) for n in range(4)))
    assert sum(not isinstance(r, Conflict) for r in results) == 1
    async with uow() as s:
        owners = (
            await s.execute(text("SELECT count(*) FROM memberships WHERE role='owner'"))
        ).scalar_one()
    assert owners == 1


async def test_guest_user_exists_with_no_password(uow: UnitOfWork, clean: None) -> None:
    await service(uow).setup(SETUP, ip=None, user_agent=None)
    guest = await PostgresAccountStore(uow).find_user_by_email(GUEST_EMAIL)
    assert guest is not None
    assert guest.is_guest
    assert guest.password_hash is None


async def test_throttle_reserves_attempts_atomically(uow: UnitOfWork, clean: None) -> None:
    throttle = PostgresLoginThrottle(uow)
    limits = ThrottleLimits(window=timedelta(minutes=15), per_email_ip=3, per_ip=20, per_email=100)
    results = await asyncio.gather(
        *(
            throttle.begin_attempt(email="x@acme.dev", ip="10.0.0.9", limits=limits)
            for _ in range(8)
        )
    )
    assert sum(r is not None for r in results) == 3  # the advisory lock serialises the check
    granted = next(r for r in results if r is not None)
    await throttle.mark_success(granted)  # successes stop counting
    assert await throttle.begin_attempt(email="x@acme.dev", ip="10.0.0.9", limits=limits)
    # A different IP for the same email isn't blocked by this IP's failures.
    assert await throttle.begin_attempt(email="x@acme.dev", ip="10.0.0.10", limits=limits)


async def test_session_groups_are_limited_to_the_users_workspace(
    uow: UnitOfWork, clean: None
) -> None:
    auth = service(uow)
    token = (await auth.setup(SETUP, ip=None, user_agent=None)).session_token
    async with uow() as s:
        other_ws = (
            await s.execute(
                text("INSERT INTO workspaces (name, slug) VALUES ('o', 'o') RETURNING id")
            )
        ).scalar_one()
        foreign = (
            await s.execute(
                text("INSERT INTO groups (workspace_id, name) VALUES (:w, 'g') RETURNING id"),
                {"w": other_ws},
            )
        ).scalar_one()
        await s.execute(
            text(
                "INSERT INTO group_members (group_id, user_id)"
                " SELECT :g, id FROM users WHERE email = 'ada@acme.dev'"
            ),
            {"g": foreign},
        )
    identity = await auth.authenticate(token)
    assert identity is not None
    assert f"group:{foreign}" not in identity.principals.values


async def test_expired_session_is_not_returned(uow: UnitOfWork, clean: None) -> None:
    auth = service(uow)
    token = (await auth.setup(SETUP, ip=None, user_agent=None)).session_token
    async with uow() as s:
        await s.execute(text("UPDATE sessions SET expires_at = now() - interval '1 second'"))
    assert await auth.authenticate(token) is None
