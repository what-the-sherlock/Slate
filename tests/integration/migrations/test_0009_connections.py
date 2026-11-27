"""Migration 0009 (ADR-0032) and the Postgres connection store, on a real Neon test branch.

Constraint tests run in a rolled-back transaction. The store test commits through the
unit of work and deletes its workspace and users at the end.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from core.adapters.postgres.connections import PostgresConnectionStore
from core.adapters.postgres.credentials import make_fernet
from core.adapters.postgres.uow import UnitOfWork
from core.domain.ids import UserId, WorkspaceId
from core.domain.integrations import Account, ConnectionStatus, TokenSet

pytestmark = pytest.mark.integration

_NEW_USER = (
    "INSERT INTO users (email, email_canonical, name, password_hash)"
    " SELECT e, e, 'C', 'h' FROM (SELECT 'c-' || gen_random_uuid() || '@t.dev' AS e) x"
    " RETURNING id"
)
_NEW_WS = "INSERT INTO workspaces (name, slug) VALUES ('c', 'c-' || gen_random_uuid()) RETURNING id"


@pytest.fixture
async def tx(direct_engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    async with direct_engine.connect() as conn:
        trans = await conn.begin()
        try:
            yield conn
        finally:
            await trans.rollback()


async def _member(conn: AsyncConnection) -> tuple[object, object]:
    ws = (await conn.execute(text(_NEW_WS))).scalar_one()
    user = (await conn.execute(text(_NEW_USER))).scalar_one()
    await conn.execute(
        text("INSERT INTO memberships (workspace_id, user_id, role) VALUES (:w, :u, 'member')"),
        {"w": ws, "u": user},
    )
    return ws, user


async def _insert(conn: AsyncConnection, ws: object, user: object, **over: object) -> None:
    row = {"p": "linear", "a": "acct", "st": "connected", **over}
    await conn.execute(
        text(
            "INSERT INTO connections (workspace_id, user_id, provider, account_id, status,"
            " secret_encrypted) VALUES (:w, :u, :p, :a, :st, '\\x00')"
        ),
        {"w": ws, "u": user, **row},
    )


async def test_a_connection_needs_a_membership(tx: AsyncConnection) -> None:
    ws = (await tx.execute(text(_NEW_WS))).scalar_one()
    stranger = (await tx.execute(text(_NEW_USER))).scalar_one()
    with pytest.raises(DBAPIError):
        await _insert(tx, ws, stranger)


async def test_leaving_the_workspace_deletes_its_connections(tx: AsyncConnection) -> None:
    ws, user = await _member(tx)
    await _insert(tx, ws, user)
    await tx.execute(
        text("DELETE FROM memberships WHERE workspace_id = :w AND user_id = :u"),
        {"w": ws, "u": user},
    )
    left = await tx.execute(
        text("SELECT count(*) FROM connections WHERE user_id = :u"), {"u": user}
    )
    assert left.scalar_one() == 0


@pytest.mark.parametrize(
    "bad",
    [{"p": "Linear"}, {"p": "x"}, {"p": "a; drop"}, {"st": "fine"}, {"a": ""}],
)
async def test_constraints_reject_bad_rows(tx: AsyncConnection, bad: dict[str, object]) -> None:
    ws, user = await _member(tx)
    with pytest.raises(DBAPIError):
        await _insert(tx, ws, user, **bad)


async def test_one_row_per_user_provider_and_account(tx: AsyncConnection) -> None:
    ws, user = await _member(tx)
    await _insert(tx, ws, user)
    with pytest.raises(DBAPIError):
        await _insert(tx, ws, user)


@pytest.fixture
async def member(uow: UnitOfWork) -> AsyncIterator[tuple[WorkspaceId, UserId]]:
    async with uow() as s:
        ws = (await s.execute(text(_NEW_WS))).scalar_one()
        user = (await s.execute(text(_NEW_USER))).scalar_one()
        await s.execute(
            text("INSERT INTO memberships (workspace_id, user_id, role) VALUES (:w, :u, 'owner')"),
            {"w": ws, "u": user},
        )
    yield WorkspaceId(UUID(str(ws))), UserId(UUID(str(user)))
    async with uow() as s:
        await s.execute(text("DELETE FROM workspaces WHERE id = :w"), {"w": ws})
        await s.execute(text("DELETE FROM users WHERE id = :u"), {"u": user})


async def test_store_round_trip_encrypts_and_scopes_by_owner(
    uow: UnitOfWork, member: tuple[WorkspaceId, UserId]
) -> None:
    ws, user = member
    store = PostgresConnectionStore(uow, make_fernet(Fernet.generate_key().decode()))
    exp = datetime.now(UTC) + timedelta(hours=1)
    tokens = TokenSet("access-secret", "refresh-secret", exp, ("read",))
    conn = await store.save(ws, user, "linear", Account("o1", "Kestrel"), tokens)
    assert conn.status is ConnectionStatus.CONNECTED

    async with uow() as s:
        raw = (
            await s.execute(
                text("SELECT secret_encrypted FROM connections WHERE id = :i"), {"i": conn.id}
            )
        ).scalar_one()
    assert b"access-secret" not in bytes(raw)  # ciphertext only
    assert await store.tokens(conn.id) == tokens

    other = UserId(UUID(int=1))
    assert await store.get(ws, other, conn.id) is None
    assert await store.delete(ws, other, conn.id) is False

    assert await store.try_lease(conn.id, datetime.now(UTC) + timedelta(seconds=60))
    assert not await store.try_lease(conn.id, datetime.now(UTC) + timedelta(seconds=60))
    await store.store_refresh(conn.id, TokenSet("access-2", "refresh-2", exp))
    assert await store.try_lease(conn.id, datetime.now(UTC) + timedelta(seconds=60))  # released
    refreshed = await store.tokens(conn.id)
    assert refreshed is not None
    assert refreshed.access_token == "access-2"

    await store.mark(conn.id, ConnectionStatus.NEEDS_RECONNECT, "invalid_grant")
    marked = await store.get(ws, user, conn.id)
    assert marked is not None
    assert (marked.status, marked.last_error) == (ConnectionStatus.NEEDS_RECONNECT, "invalid_grant")

    again = await store.save(ws, user, "linear", Account("o1", "Kestrel"), tokens)  # reconnect
    assert again.id == conn.id
    assert again.status is ConnectionStatus.CONNECTED
    assert await store.delete(ws, user, conn.id) is True
    assert await store.list_for_user(ws, user) == []
