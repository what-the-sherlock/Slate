"""Migration 0006 (ADR-0026): public data only where the workspace allows it, connector-only
ACLs for GitHub, and the data migration that un-publishes user uploads.

Every test runs in a transaction that is rolled back. The data-migration test replays the
migration's SQL on rows planted in the pre-0006 shape (guards disabled inside the
transaction only), so it proves exactly what ``alembic upgrade`` does to old data.
"""

from __future__ import annotations

import importlib
from collections.abc import AsyncIterator
from types import ModuleType
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

pytestmark = pytest.mark.integration

_M: ModuleType = importlib.import_module("migrations.versions.0006_private_by_default")


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


async def workspace(conn: AsyncConnection, *, allows_public: bool = False) -> object:
    return await scalar(
        conn,
        "INSERT INTO workspaces (name, slug, allows_public)"
        " VALUES ('w', 'w-' || gen_random_uuid(), :p) RETURNING id",
        p=allows_public,
    )


async def owner(conn: AsyncConnection, ws: object) -> object:
    uid = await scalar(
        conn,
        "INSERT INTO users (email, email_canonical, name, password_hash)"
        " VALUES (:e, :e, 'n', 'h') RETURNING id",
        e=f"{uuid4().hex}@example.com",
    )
    await conn.execute(
        text("INSERT INTO memberships (workspace_id, user_id, role) VALUES (:w, :u, 'owner')"),
        {"w": ws, "u": uid},
    )
    return uid


async def source(
    conn: AsyncConnection, ws: object, *, name: str, connector: str, vis: list[str], dc: str
) -> object:
    return await scalar(
        conn,
        "INSERT INTO sources (workspace_id, connector, name, visibility, data_class)"
        " VALUES (:ws, :c, :n, CAST(:v AS text[]), :dc) RETURNING id",
        ws=ws, c=connector, n=name, v=vis, dc=dc,
    )  # fmt: skip


async def document(
    conn: AsyncConnection, ws: object, src: object, *, ext: str, acl: list[str], dc: str
) -> object:
    doc = await scalar(
        conn,
        "INSERT INTO documents (workspace_id, source_id, external_id, uri, title, mime,"
        " checksum, acl, data_class) VALUES (:ws, :s, :e, 'u', 't', 'text/plain', 'c',"
        " CAST(:a AS text[]), :dc) RETURNING id",
        ws=ws, s=src, e=ext, a=acl, dc=dc,
    )  # fmt: skip
    await conn.execute(
        text(
            "INSERT INTO chunks (document_id, workspace_id, ordinal, content, token_count, acl,"
            " data_class) VALUES (:d, :ws, 0, 'x', 1, CAST(:a AS text[]), :dc)"
        ),
        {"d": doc, "ws": ws, "a": acl, "dc": dc},
    )
    return doc


def _violates(err: DBAPIError, name: str) -> bool:
    return name in str(err.orig)


async def test_public_source_refused_outside_allowing_workspace(tx: AsyncConnection) -> None:
    ws = await workspace(tx)
    with pytest.raises(DBAPIError) as e:
        async with tx.begin_nested():
            await source(tx, ws, name="p", connector="upload", vis=["public"], dc="public")
    assert _violates(e.value, "sources_public_only_where_allowed")


async def test_public_principal_alone_is_refused_too(tx: AsyncConnection) -> None:
    """Search filters by workspace first, so a 'public' entry in a user workspace leaks
    nothing today; it must still not exist, so no future query can trip over it."""
    ws = await workspace(tx)
    with pytest.raises(DBAPIError):
        async with tx.begin_nested():
            await source(tx, ws, name="p", connector="github", vis=["public"], dc="private")
    src = await source(tx, ws, name="s", connector="upload", vis=[f"workspace:{ws}"], dc="private")
    with pytest.raises(DBAPIError) as e:
        async with tx.begin_nested():
            await document(tx, ws, src, ext="e", acl=["public", f"workspace:{ws}"], dc="private")
    assert _violates(e.value, "documents_public_only_where_allowed")


async def test_making_a_source_public_later_is_refused(tx: AsyncConnection) -> None:
    ws = await workspace(tx)
    src = await source(tx, ws, name="s", connector="upload", vis=[f"workspace:{ws}"], dc="private")
    with pytest.raises(DBAPIError):
        async with tx.begin_nested():
            await tx.execute(
                text("UPDATE sources SET visibility = ARRAY['public'] WHERE id = :s"), {"s": src}
            )


async def test_eval_style_workspace_may_hold_public_data(tx: AsyncConnection) -> None:
    ws = await workspace(tx, allows_public=True)
    src = await source(tx, ws, name="p", connector="upload", vis=["public"], dc="public")
    await document(tx, ws, src, ext="e", acl=["public"], dc="public")


async def test_workspace_cannot_stop_allowing_public_while_holding_it(tx: AsyncConnection) -> None:
    ws = await workspace(tx, allows_public=True)
    await source(tx, ws, name="p", connector="upload", vis=["public"], dc="public")
    with pytest.raises(DBAPIError) as e:
        async with tx.begin_nested():
            await tx.execute(
                text("UPDATE workspaces SET allows_public = false WHERE id = :w"), {"w": ws}
            )
    assert _violates(e.value, "workspaces_keep_allows_public")
    empty = await workspace(tx, allows_public=True)
    await tx.execute(
        text("UPDATE workspaces SET allows_public = false WHERE id = :w"), {"w": empty}
    )


async def test_created_by_is_nulled_when_the_user_goes(tx: AsyncConnection) -> None:
    ws = await workspace(tx)
    uid = await owner(tx, ws)
    src = await scalar(
        tx,
        "INSERT INTO sources (workspace_id, connector, name, visibility, created_by)"
        " VALUES (:w, 'github', 'o/r', CAST(:v AS text[]), :u) RETURNING id",
        w=ws, v=[f"user:{uid}"], u=uid,
    )  # fmt: skip
    await tx.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})
    assert await scalar(tx, "SELECT created_by FROM sources WHERE id = :s", s=src) is None


async def test_data_migration_unpublishes_uploads_and_narrows_github(tx: AsyncConnection) -> None:
    # Plant the pre-0006 shape: a workspace holding public uploads and a workspace-wide repo.
    ws = await workspace(tx, allows_public=True)
    uid = await owner(tx, ws)
    public = await source(
        tx, ws, name="Public uploads", connector="upload",
        vis=["public", f"workspace:{ws}"], dc="public",
    )  # fmt: skip
    pub_doc = await document(
        tx, ws, public, ext="upload:a", acl=["public", f"workspace:{ws}"], dc="public"
    )
    repo = await source(
        tx, ws, name="o/r", connector="github", vis=[f"workspace:{ws}"], dc="private"
    )
    repo_doc = await document(
        tx, ws, repo, ext="file:README.md", acl=[f"workspace:{ws}"], dc="private"
    )
    gen = await scalar(
        tx,
        "INSERT INTO embedding_generations (model, dims, status, data_scope)"
        " VALUES ('bge-test', 1024, 'past', 'public') RETURNING id",
    )
    await tx.execute(
        text(
            "INSERT INTO chunk_embeddings (chunk_id, generation_id, embedding)"
            " SELECT c.id, :g, CAST(array_fill(0.1, ARRAY[1024]) AS halfvec(1024))"
            " FROM chunks c WHERE c.document_id = :d"
        ),
        {"g": gen, "d": pub_doc},
    )
    # Now it's an ordinary user workspace (as every one was before 0006).
    await tx.execute(text("ALTER TABLE workspaces DISABLE TRIGGER workspaces_keep_allows_public"))
    await tx.execute(text("UPDATE workspaces SET allows_public = false WHERE id = :w"), {"w": ws})
    await tx.execute(text("ALTER TABLE workspaces ENABLE TRIGGER workspaces_keep_allows_public"))

    await tx.execute(text(_M._GITHUB_TO_CONNECTOR))
    await tx.execute(text(_M._UNPUBLISH_USER_DATA))

    moved = (
        await tx.execute(
            text(
                "SELECT s.name, d.data_class, d.acl FROM documents d"
                " JOIN sources s ON s.id = d.source_id WHERE d.id = :d"
            ),
            {"d": pub_doc},
        )
    ).one()
    assert moved[0] == "Uploads"
    assert moved[1] == "private"
    assert list(moved[2]) == [f"workspace:{ws}"]
    chunk = (
        await tx.execute(
            text("SELECT data_class, acl FROM chunks WHERE document_id = :d"), {"d": pub_doc}
        )
    ).one()
    assert chunk[0] == "private"
    assert "public" not in chunk[1]
    free_tier = await scalar(
        tx,
        "SELECT count(*) FROM chunk_embeddings e JOIN chunks c ON c.id = e.chunk_id"
        " WHERE c.document_id = :d",
        d=pub_doc,
    )
    assert free_tier == 0
    assert await scalar(tx, "SELECT count(*) FROM sources WHERE id = :s", s=public) == 0, (
        "the empty public source is removed"
    )
    narrowed = (
        await tx.execute(
            text("SELECT s.visibility, s.created_by, d.acl FROM sources s"
                 " JOIN documents d ON d.source_id = s.id WHERE d.id = :d"),
            {"d": repo_doc},
        )
    ).one()  # fmt: skip
    assert list(narrowed[0]) == [f"user:{uid}"]
    assert narrowed[1] == uid
    assert list(narrowed[2]) == [f"user:{uid}"]
    repo_chunk_acl = await scalar(tx, "SELECT acl FROM chunks WHERE document_id = :d", d=repo_doc)
    assert list(repo_chunk_acl) == [f"user:{uid}"]  # type: ignore[call-overload]


async def test_data_migration_is_idempotent(tx: AsyncConnection) -> None:
    """Re-running the data steps changes nothing (a partly applied run can be retried)."""
    await tx.execute(text(_M._GITHUB_TO_CONNECTOR))
    await tx.execute(text(_M._UNPUBLISH_USER_DATA))
    leaks = await scalar(
        tx,
        "SELECT count(*) FROM documents d JOIN workspaces w ON w.id = d.workspace_id"
        " WHERE NOT w.allows_public AND (d.data_class = 'public' OR 'public' = ANY (d.acl))",
    )
    assert leaks == 0
