"""Migration 0017 (ADR-0043): an attachment child is never more visible than its parent.

Each test runs in a transaction that is rolled back.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

pytestmark = pytest.mark.integration


@pytest.fixture
async def tx(direct_engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    async with direct_engine.connect() as conn:
        trans = await conn.begin()
        try:
            yield conn
        finally:
            await trans.rollback()


async def scalar(conn: AsyncConnection, sql: str, **params: object) -> object:
    return (await conn.execute(text(sql), params)).scalar_one()


async def source(conn: AsyncConnection, connector: str = "imap") -> tuple[object, object]:
    """A connector source's documents take its visibility (migration 0008); an "upload"
    source lets each document keep its own ACL, which the propagation test needs."""
    ws = await scalar(
        conn,
        "INSERT INTO workspaces (name, slug) VALUES ('w', 'w-' || gen_random_uuid()) RETURNING id",
    )
    src = await scalar(
        conn,
        "INSERT INTO sources (workspace_id, connector, name, visibility, data_class)"
        " VALUES (:ws, :c, 'mail', ARRAY['user:a'], 'private') RETURNING id",
        ws=ws, c=connector,
    )  # fmt: skip
    return ws, src


async def doc(
    conn: AsyncConnection,
    ws: object,
    src: object,
    ext: str,
    acl: list[str],
    parent: object | None = None,
) -> object:
    d = await scalar(
        conn,
        "INSERT INTO documents (workspace_id, source_id, external_id, uri, title, mime,"
        " checksum, acl, data_class, parent_document_id) VALUES (:ws, :s, :e, 'u', 't',"
        " 'text/plain', 'c', CAST(:a AS text[]), 'private', :p) RETURNING id",
        ws=ws, s=src, e=ext, a=acl, p=parent,
    )  # fmt: skip
    await conn.execute(
        text(
            "INSERT INTO chunks (document_id, workspace_id, ordinal, content, token_count, acl,"
            " data_class) VALUES (:d, :ws, 0, 'x', 1, '{}', 'private')"
        ),
        {"d": d, "ws": ws},
    )
    return d


async def test_child_takes_parent_acl_whatever_it_asks_for(tx: AsyncConnection) -> None:
    ws, src = await source(tx)
    parent = await doc(tx, ws, src, "thread:1", ["user:a"])
    child = await doc(tx, ws, src, "thread:1#att/0", ["public", "user:evil"], parent)
    assert await scalar(tx, "SELECT acl FROM documents WHERE id = :d", d=child) == ["user:a"]
    assert await scalar(tx, "SELECT acl FROM chunks WHERE document_id = :d", d=child) == ["user:a"]
    await tx.execute(
        text("UPDATE documents SET acl = ARRAY['user:evil'] WHERE id = :d"), {"d": child}
    )
    assert await scalar(tx, "SELECT acl FROM documents WHERE id = :d", d=child) == ["user:a"]


async def test_parent_acl_and_deletion_flow_down(tx: AsyncConnection) -> None:
    ws, src = await source(tx, "upload")
    parent = await doc(tx, ws, src, "thread:1", ["user:a"])
    child = await doc(tx, ws, src, "thread:1#att/0", ["user:a"], parent)
    await tx.execute(
        text("UPDATE documents SET acl = ARRAY['user:b'] WHERE id = :d"), {"d": parent}
    )
    assert await scalar(tx, "SELECT acl FROM chunks WHERE document_id = :d", d=child) == ["user:b"]
    await tx.execute(text("UPDATE documents SET deleted_at = now() WHERE id = :d"), {"d": parent})
    assert await scalar(tx, "SELECT deleted_at IS NOT NULL FROM documents WHERE id = :d", d=child)
    await tx.execute(text("DELETE FROM documents WHERE id = :d"), {"d": parent})
    assert await scalar(tx, "SELECT count(*) FROM documents WHERE id = :d", d=child) == 0
    assert await scalar(tx, "SELECT count(*) FROM chunks WHERE document_id = :d", d=child) == 0


async def test_child_must_share_parent_source(tx: AsyncConnection) -> None:
    ws, src = await source(tx)
    other = await scalar(
        tx,
        "INSERT INTO sources (workspace_id, connector, name, visibility, data_class)"
        " VALUES (:ws, 'imap', 'm2', ARRAY['user:a'], 'private') RETURNING id",
        ws=ws,
    )
    parent = await doc(tx, ws, src, "thread:1", ["user:a"])
    with pytest.raises(DBAPIError) as e:
        await doc(tx, ws, other, "x#att/0", ["user:a"], parent)
    assert _constraint(e.value) == "documents_parent_source"


async def test_only_one_level(tx: AsyncConnection) -> None:
    ws, src = await source(tx)
    parent = await doc(tx, ws, src, "thread:1", ["user:a"])
    child = await doc(tx, ws, src, "thread:1#att/0", ["user:a"], parent)
    with pytest.raises(DBAPIError) as e:
        await doc(tx, ws, src, "thread:1#att/0#att/0", ["user:a"], child)
    assert _constraint(e.value) == "documents_one_level"


async def test_child_cannot_cross_workspaces(tx: AsyncConnection) -> None:
    ws, src = await source(tx)
    ws2, src2 = await source(tx)
    parent = await doc(tx, ws, src, "thread:1", ["user:a"])
    with pytest.raises(DBAPIError):
        await doc(tx, ws2, src2, "thread:1#att/0", ["user:a"], parent)


def _constraint(err: DBAPIError) -> str | None:
    """psycopg puts the constraint name in diagnostics, not in the message text."""
    return getattr(getattr(err.orig, "diag", None), "constraint_name", None)


async def test_a_connector_child_follows_its_sources_visibility(tx: AsyncConnection) -> None:
    """The real path for email and Discord: sharing changes the source, then documents
    are rewritten; parent and child both end up with the source's visibility."""
    ws, src = await source(tx)
    parent = await doc(tx, ws, src, "thread:1", ["user:a"])
    child = await doc(tx, ws, src, "thread:1#att/0", ["user:a"], parent)
    await tx.execute(
        text("UPDATE sources SET visibility = ARRAY['user:a', 'workspace:x'] WHERE id = :s"),
        {"s": src},
    )
    await tx.execute(text("UPDATE documents SET acl = acl WHERE id = :d"), {"d": parent})
    want = ["user:a", "workspace:x"]
    assert await scalar(tx, "SELECT acl FROM documents WHERE id = :d", d=child) == want
    assert await scalar(tx, "SELECT acl FROM chunks WHERE document_id = :d", d=child) == want
