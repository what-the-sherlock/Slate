"""Migration 0010 (ADR-0033) on a real Neon test branch, in rolled-back transactions."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

pytestmark = pytest.mark.integration

_NEW_USER = (
    "INSERT INTO users (email, email_canonical, name, password_hash)"
    " SELECT e, e, 'C', 'h' FROM (SELECT 'c-' || gen_random_uuid() || '@t.dev' AS e) x"
    " RETURNING id"
)


@pytest.fixture
async def tx(direct_engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    async with direct_engine.connect() as conn:
        trans = await conn.begin()
        try:
            yield conn
        finally:
            await trans.rollback()


async def _connection(conn: AsyncConnection) -> tuple[object, object, object]:
    ws = (
        await conn.execute(
            text(
                "INSERT INTO workspaces (name, slug) VALUES ('c', 'c-' || gen_random_uuid()) RETURNING id"
            )
        )
    ).scalar_one()
    user = (await conn.execute(text(_NEW_USER))).scalar_one()
    await conn.execute(
        text("INSERT INTO memberships (workspace_id, user_id, role) VALUES (:w, :u, 'member')"),
        {"w": ws, "u": user},
    )
    cid = (
        await conn.execute(
            text(
                "INSERT INTO connections (workspace_id, user_id, provider, account_id,"
                " secret_encrypted) VALUES (:w, :u, 'gmail', 'acct', '\\x00') RETURNING id"
            ),
            {"w": ws, "u": user},
        )
    ).scalar_one()
    return ws, user, cid


async def _source(
    conn: AsyncConnection, ws: object, user: object, connection: object, *, connector: str = "gmail",
    data_class: str = "private",
) -> object:  # fmt: skip
    return (
        await conn.execute(
            text(
                "INSERT INTO sources (workspace_id, connector, name, visibility, data_class,"
                " created_by, connection_id) VALUES (:w, :c, 'Gmail · a-' || gen_random_uuid(),"
                " ARRAY['user:' || CAST(:u AS text)], :dc, :u, :conn) RETURNING id"
            ),
            {"w": ws, "c": connector, "u": user, "dc": data_class, "conn": connection},
        )
    ).scalar_one()


async def test_disconnecting_cascades_to_the_source_and_its_documents(tx: AsyncConnection) -> None:
    ws, user, cid = await _connection(tx)
    sid = await _source(tx, ws, user, cid)
    await tx.execute(
        text(
            "INSERT INTO documents (workspace_id, source_id, external_id, uri, title, mime,"
            " checksum, acl, data_class) VALUES (:w, :s, 'thread:1', 'u', 't', 'm', 'c',"
            " ARRAY['user:' || CAST(:u AS text)], 'private')"
        ),
        {"w": ws, "s": sid, "u": user},
    )
    await tx.execute(text("DELETE FROM connections WHERE id = :c"), {"c": cid})
    left = await tx.execute(
        text(
            "SELECT (SELECT count(*) FROM sources WHERE id = :s), (SELECT count(*) FROM documents WHERE source_id = :s)"
        ),
        {"s": sid},
    )
    assert tuple(left.one()) == (0, 0)


async def test_a_connected_source_needs_its_connection(tx: AsyncConnection) -> None:
    ws, user, _ = await _connection(tx)
    with pytest.raises(DBAPIError):
        await _source(tx, ws, user, None, connector="notion")


async def test_gmail_is_always_private(tx: AsyncConnection) -> None:
    ws, user, cid = await _connection(tx)
    await tx.execute(text("UPDATE workspaces SET allows_public = true WHERE id = :w"), {"w": ws})
    with pytest.raises(DBAPIError):
        await _source(tx, ws, user, cid, data_class="public")


async def test_one_source_per_connection(tx: AsyncConnection) -> None:
    ws, user, cid = await _connection(tx)
    await _source(tx, ws, user, cid)
    with pytest.raises(DBAPIError):
        await _source(tx, ws, user, cid)


async def test_every_new_connector_name_is_allowed(tx: AsyncConnection) -> None:
    for name in ("notion", "linear", "google_calendar", "google_drive", "slack"):
        ws, user, cid = await _connection(tx)
        assert await _source(tx, ws, user, cid, connector=name)
