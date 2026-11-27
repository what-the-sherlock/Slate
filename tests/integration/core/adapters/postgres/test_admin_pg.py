"""Admin console SQL against a real database (ADR-0042).

Not destructive: each test creates accounts under its own random email tag and deletes
them afterwards. Platform-wide counts are compared as deltas, since a shared test branch
may hold other accounts.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text

from core.adapters.postgres.admin import PostgresAdminStore, PostgresAuthEventLog, escape_like
from core.adapters.postgres.auth import PostgresAccountStore, PostgresSessionStore
from core.adapters.postgres.uow import UnitOfWork
from core.domain.admin import AuthEventKind, Bucket, UserSort, UserStatus
from core.domain.auth import hash_token
from core.domain.ids import UserId


class Run:
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


async def _user(uow: UnitOfWork, email: str) -> UserId:
    _, uid = await PostgresAccountStore(uow).create_user_with_workspace(
        email=email, email_canonical=email, name="Test", password_hash="x",
        workspace_name="T", slug=f"ws-{uuid4().hex[:12]}",
    )  # fmt: skip
    return uid


async def test_overview_and_series_count_new_accounts(uow: UnitOfWork, run: Run) -> None:
    store = PostgresAdminStore(uow)
    now = datetime.now(UTC)
    before = await store.overview(now)
    uid = await _user(uow, run.email("a"))
    await PostgresAccountStore(uow).mark_email_verified(uid)
    await _user(uow, run.email("b"))
    after = await store.overview(datetime.now(UTC))
    assert after.total - before.total == 2
    assert after.verified - before.verified == 1
    assert after.new_today - before.new_today == 2
    counts = await store.signup_counts(now.date() - timedelta(days=1), Bucket.DAY)
    assert counts.get(now.date(), 0) >= 2


async def test_list_users_search_filter_and_last_active(uow: UnitOfWork, run: Run) -> None:
    store, sessions = PostgresAdminStore(uow), PostgresSessionStore(uow)
    a = await _user(uow, run.email("alpha"))
    await _user(uow, run.email("beta"))
    # Signing in (session create) marks the account active; it outlives the session.
    created = await sessions.create(
        user_id=a, token_hash=hash_token(uuid4().hex), csrf_secret="c",
        ttl=timedelta(hours=1), ip=None, user_agent=None,
    )  # fmt: skip
    assert created is not None
    await sessions.delete_for_user(a)
    users, total = await store.list_users(
        query=run.tag, status=UserStatus.ALL, sort=UserSort.EMAIL, descending=False,
        offset=0, limit=10, now=datetime.now(UTC),
    )  # fmt: skip
    assert total == 2
    assert [u.email for u in users] == [run.email("alpha"), run.email("beta")]
    assert users[0].last_active_at is not None
    assert users[0].workspaces == 1
    active, _ = await store.list_users(
        query=run.tag, status=UserStatus.ACTIVE, sort=UserSort.LAST_ACTIVE, descending=True,
        offset=0, limit=10, now=datetime.now(UTC),
    )  # fmt: skip
    assert [u.id for u in active] == [a]


async def test_like_wildcards_are_literal(uow: UnitOfWork, run: Run) -> None:
    await _user(uow, run.email("x"))
    users, total = await PostgresAdminStore(uow).list_users(
        query="%", status=UserStatus.ALL, sort=UserSort.JOINED, descending=True,
        offset=0, limit=5, now=datetime.now(UTC),
    )  # fmt: skip
    assert all("%" in u.email or "%" in u.name for u in users)
    assert escape_like("a_%\\") == "a\\_\\%\\\\"
    assert total == len(users)


async def test_disable_events_and_retention(uow: UnitOfWork, run: Run) -> None:
    store, events = PostgresAdminStore(uow), PostgresAuthEventLog(uow)
    uid = await _user(uow, run.email("d"))
    assert await store.set_disabled(uid, True, actor_id=uid)
    assert not await store.set_disabled(uid, True, actor_id=uid)  # unchanged: no 2nd event
    got = await store.get_user(uid)
    assert got is not None
    assert got.disabled
    page = await store.list_events(kind=AuthEventKind.ACCOUNT_DISABLED, before_id=None, limit=5)
    mine = [e for e in page if e.user_id == uid]
    assert len(mine) == 1
    assert mine[0].user_email == run.email("d")
    assert await store.set_disabled(uid, False, actor_id=uid)
    async with uow() as s:
        await s.execute(
            text(
                "UPDATE auth_events SET created_at = now() - interval '91 days' WHERE user_id = :u"
            ),
            {"u": uid},
        )
    assert await events.delete_before(datetime.now(UTC) - timedelta(days=90)) >= 1


async def test_delete_user_cascades_and_blocks_shared_owner(uow: UnitOfWork, run: Run) -> None:
    from core.domain.admin import DeleteOutcome

    store = PostgresAdminStore(uow)
    gone = await _user(uow, run.email("gone"))
    host = await _user(uow, run.email("host"))
    guest = await _user(uow, run.email("guest"))
    async with uow() as s:
        ws = (
            await s.execute(
                text("SELECT workspace_id FROM memberships WHERE user_id = :u"), {"u": host}
            )
        ).scalar_one()
        await s.execute(
            text("INSERT INTO memberships (workspace_id, user_id, role) VALUES (:w, :u, 'member')"),
            {"w": ws, "u": guest},
        )
    assert await store.delete_user(host, actor_id=gone) is DeleteOutcome.OWNS_SHARED_WORKSPACE
    assert await store.get_user(host) is not None  # nothing changed
    # A guest can be deleted: their membership in host's workspace goes, host's stays.
    assert await store.delete_user(guest, actor_id=host) is DeleteOutcome.DELETED
    assert await store.get_user(guest) is None
    assert await store.delete_user(host, actor_id=gone) is DeleteOutcome.DELETED
    async with uow() as s:
        left = (
            await s.execute(text("SELECT count(*) FROM workspaces WHERE id = :w"), {"w": ws})
        ).scalar_one()
    assert left == 0
    assert await store.delete_user(host, actor_id=gone) is DeleteOutcome.NOT_FOUND
    assert await store.delete_user(gone, actor_id=gone) is DeleteOutcome.DELETED
