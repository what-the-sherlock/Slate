"""Shared workspaces against real Postgres (ADR-0030, migration 0008).

Commits through the unit of work; each test creates its own users and workspaces and
deletes them at the end (memberships, sources, documents, sessions cascade).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from core.adapters.postgres.auth import PostgresSessionStore
from core.adapters.postgres.documents import PostgresDocumentStore
from core.adapters.postgres.sources import PostgresSourceStore
from core.adapters.postgres.uow import UnitOfWork
from core.adapters.postgres.workspaces import PostgresWorkspaceStore
from core.domain.auth import Role
from core.domain.data_class import DataClass
from core.domain.documents import DocumentUpsert
from core.domain.ids import UserId, WorkspaceId
from core.domain.principals import user_principal, workspace_principal
from core.domain.workspaces import new_invite_token

pytestmark = pytest.mark.integration


@dataclass
class World:
    owner: UserId
    owner_ws: WorkspaceId
    bob: UserId
    bob_ws: WorkspaceId


async def _user_with_workspace(uow: UnitOfWork, tag: str) -> tuple[UserId, WorkspaceId]:
    async with uow() as s:
        row = (
            await s.execute(
                text(
                    "WITH u AS (INSERT INTO users (email, email_canonical, name, password_hash,"
                    " email_verified_at) VALUES (:e, :e, :n, 'h', now()) RETURNING id),"
                    " w AS (INSERT INTO workspaces (name, slug) VALUES (:n, 'ws-' || gen_random_uuid())"
                    " RETURNING id),"
                    " m AS (INSERT INTO memberships (workspace_id, user_id, role)"
                    "   SELECT w.id, u.id, 'owner' FROM u, w RETURNING workspace_id, user_id)"
                    " SELECT user_id, workspace_id FROM m"
                ),
                {"e": f"{tag}-{uuid4().hex[:8]}@it.test", "n": tag},
            )
        ).one()
    return UserId(UUID(str(row[0]))), WorkspaceId(UUID(str(row[1])))


@pytest.fixture
async def world(uow: UnitOfWork) -> AsyncIterator[World]:
    owner, owner_ws = await _user_with_workspace(uow, "owner")
    bob, bob_ws = await _user_with_workspace(uow, "bob")
    yield World(owner, owner_ws, bob, bob_ws)
    async with uow() as s:
        await s.execute(
            text("DELETE FROM workspaces WHERE id = ANY(:w)"), {"w": [owner_ws, bob_ws]}
        )
        await s.execute(text("DELETE FROM users WHERE id = ANY(:u)"), {"u": [owner, bob]})


async def _join(uow: UnitOfWork, w: World, role: Role = Role.MEMBER) -> None:
    store = PostgresWorkspaceStore(uow)
    _token, digest = new_invite_token()
    async with uow() as s:
        email = (
            await s.execute(text("SELECT email_canonical FROM users WHERE id = :u"), {"u": w.bob})
        ).scalar_one()
    iid = await store.create_invite(
        w.owner_ws, email_canonical=str(email), role=role, token_hash=digest, invited_by=w.owner,
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )  # fmt: skip
    assert await store.accept_invite(iid, w.bob)
    assert not await store.accept_invite(iid, w.bob)  # single use


async def test_session_follows_active_workspace_and_falls_back(
    uow: UnitOfWork, world: World
) -> None:
    sessions = PostgresSessionStore(uow)
    token = uuid4().bytes * 2
    rec = await sessions.create(
        user_id=world.bob, token_hash=token, csrf_secret="c", ttl=timedelta(hours=1), ip=None, user_agent=None,
    )  # fmt: skip
    assert rec is not None
    assert rec.workspace_id == world.bob_ws  # their own workspace first
    assert not await sessions.set_workspace(rec.id, world.owner_ws)  # not a member yet
    await _join(uow, world)
    assert await sessions.set_workspace(rec.id, world.owner_ws)
    now = await sessions.get(token)
    assert now is not None
    assert (now.workspace_id, now.role) == (world.owner_ws, Role.MEMBER)
    await PostgresWorkspaceStore(uow).remove_member(world.owner_ws, world.bob)
    after = await sessions.get(token)
    assert after is not None
    assert after.workspace_id == world.bob_ws  # removal takes effect on the very next request


async def test_removing_a_member_deletes_what_they_connected_there(
    uow: UnitOfWork, world: World
) -> None:
    await _join(uow, world)
    sources = PostgresSourceStore(uow)
    theirs = await sources.create_source(
        world.owner_ws, connector="github", name=f"bob/{uuid4().hex[:6]}", config={},
        visibility=[user_principal(world.bob)], data_class=DataClass.PRIVATE, created_by=world.bob,
    )  # fmt: skip
    mine = await sources.create_source(
        world.owner_ws, connector="github", name=f"owner/{uuid4().hex[:6]}", config={},
        visibility=[user_principal(world.owner)], data_class=DataClass.PRIVATE, created_by=world.owner,
    )  # fmt: skip
    store = PostgresWorkspaceStore(uow)
    assert await store.remove_member(world.owner_ws, world.owner) == 0  # the owner never goes
    assert await store.remove_member(world.owner_ws, world.bob) == 1
    assert await sources.get_source(world.owner_ws, theirs) is None
    assert await sources.get_source(world.owner_ws, mine) is not None
    assert await store.role_of(world.owner_ws, world.bob) is None


async def test_sharing_widens_and_narrows_documents_and_chunks(
    uow: UnitOfWork, world: World
) -> None:
    sources = PostgresSourceStore(uow)
    sid = await sources.create_source(
        world.owner_ws, connector="discord", name=f"Acme {uuid4().hex[:6]}", config={},
        visibility=[user_principal(world.owner)], data_class=DataClass.PRIVATE, created_by=world.owner,
    )  # fmt: skip
    await PostgresDocumentStore(uow).upsert_document(
        world.owner_ws,
        DocumentUpsert(sid, "msg:1:2", "u", "t", "text/plain", "c", (user_principal(world.owner),)),
    )
    store = PostgresWorkspaceStore(uow)
    assert not await store.set_shared(world.owner_ws, sid, world.bob, True)  # not theirs
    assert await store.set_shared(world.owner_ws, sid, world.owner, True)
    async with uow() as s:
        acl = (
            await s.execute(text("SELECT acl FROM documents WHERE source_id = :s"), {"s": sid})
        ).scalar_one()
    assert set(acl) == {user_principal(world.owner), workspace_principal(world.owner_ws)}
    assert await store.set_shared(world.owner_ws, sid, world.owner, False)
    async with uow() as s:
        acl = (
            await s.execute(text("SELECT acl FROM documents WHERE source_id = :s"), {"s": sid})
        ).scalar_one()
    assert list(acl) == [user_principal(world.owner)]


async def test_one_open_invite_per_address_and_expiry(uow: UnitOfWork, world: World) -> None:
    store = PostgresWorkspaceStore(uow)
    _t1, h1 = new_invite_token()
    _t2, h2 = new_invite_token()
    first = await store.create_invite(
        world.owner_ws, email_canonical="x@it.test", role=Role.VIEWER, token_hash=h1,
        invited_by=world.owner, expires_at=datetime.now(UTC) + timedelta(days=1),
    )  # fmt: skip
    await store.create_invite(
        world.owner_ws, email_canonical="x@it.test", role=Role.VIEWER, token_hash=h2,
        invited_by=world.owner, expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )  # fmt: skip
    old = await store.find_invite(h1)
    assert old is not None
    assert not old.open  # replaced by the resend
    assert not await store.accept_invite(first, world.bob)
    expired = await store.find_invite(h2)
    assert expired is not None
    assert not await store.accept_invite(expired.id, world.bob)
    assert await store.invites(world.owner_ws) == []
