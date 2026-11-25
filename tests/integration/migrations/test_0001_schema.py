"""Migration 0001: structure exists and the A1-A4 rules are enforced by the database.

Every test runs in a transaction that is rolled back, so nothing is left on the branch.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

TABLES = {
    "workspaces", "users", "memberships", "groups", "group_members", "sessions", "api_keys",
    "credentials", "sources", "ingestion_runs", "ingestion_errors", "documents", "chunks",
    "embedding_generations", "chunk_embeddings", "chat_sessions", "messages",
    "message_citations", "feedback", "jobs", "audit_log", "usage_ledger", "eval_cases",
    "eval_runs",
}  # fmt: skip
INDEXES = {
    "chunks_acl_gin", "chunks_tsv_en_gin", "chunks_tsv_simple_gin",
    "chunks_workspace_document_idx", "documents_workspace_source_idx", "jobs_runnable_idx",
    "jobs_running_heartbeat_idx", "embedding_generations_one_present",
    "embedding_generations_one_future", "ingestion_runs_source_idx", "sessions_expires_idx",
    "chat_sessions_user_idx", "messages_session_idx", "audit_log_workspace_idx",
}  # fmt: skip

NEW_WORKSPACE = (
    "INSERT INTO workspaces (name, slug) VALUES ('t', 't-' || gen_random_uuid()) RETURNING id"
)
NEW_SOURCE = (
    "INSERT INTO sources (workspace_id, connector, name, visibility, data_class) "
    "VALUES (:ws, :c, 's', CAST(:v AS text[]), :dc) RETURNING id"
)
NEW_DOCUMENT = (
    "INSERT INTO documents (workspace_id, source_id, external_id, uri, title, mime, checksum, acl)"
    " VALUES (:ws, :src, 'e', 'u', 't', 'text/plain', 'c', CAST(:acl AS text[])) RETURNING id"
)
NEW_GENERATION = "INSERT INTO embedding_generations (model, dims, status) VALUES (:m, 1024, :s)"


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


async def workspace_and_source(
    conn: AsyncConnection,
    *,
    connector: str = "github",
    visibility: str = "{public}",
    data_class: str = "private",
) -> tuple[object, object]:
    ws = await scalar(conn, NEW_WORKSPACE)
    src = await scalar(conn, NEW_SOURCE, ws=ws, c=connector, v=visibility, dc=data_class)
    return ws, src


async def test_all_tables_indexes_and_extensions_exist(tx: AsyncConnection) -> None:
    q = "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
    assert {r[0] for r in await tx.execute(text(q))} >= TABLES
    q = "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"
    assert {r[0] for r in await tx.execute(text(q))} >= INDEXES
    exts = {r[0] for r in await tx.execute(text("SELECT extname FROM pg_extension"))}
    assert {"vector", "citext"} <= exts


# ---------------------------------------------------------------- A1: data isolation


async def test_sources_default_to_private(tx: AsyncConnection) -> None:
    ws = await scalar(tx, NEW_WORKSPACE)
    dc = await scalar(
        tx,
        "INSERT INTO sources (workspace_id, connector, name, visibility) "
        "VALUES (:ws, 'upload', 'u', '{public}') RETURNING data_class",
        ws=ws,
    )
    assert dc == "private"


async def test_public_data_class_requires_public_acl(tx: AsyncConnection) -> None:
    with pytest.raises(IntegrityError, match="sources_public_requires_public_acl"):
        await workspace_and_source(tx, visibility="{workspace:x}", data_class="public")


async def test_gmail_can_never_be_public(tx: AsyncConnection) -> None:
    with pytest.raises(IntegrityError, match="sources_gmail_always_private"):
        await workspace_and_source(tx, connector="gmail_mbox", data_class="public")


async def test_chunks_enforce_the_same_isolation_rule(tx: AsyncConnection) -> None:
    ws, src = await workspace_and_source(tx)
    doc = await scalar(tx, NEW_DOCUMENT, ws=ws, src=src, acl="{user:x}")
    with pytest.raises(IntegrityError, match="chunks_public_requires_public_acl"):
        await tx.execute(
            text(
                "INSERT INTO chunks (document_id, workspace_id, ordinal, content, token_count,"
                " acl, data_class) VALUES (:d, :ws, 0, 'secret', 1, '{user:x}', 'public')"
            ),
            {"d": doc, "ws": ws},
        )


async def test_usage_ledger_requires_a_data_class(tx: AsyncConnection) -> None:
    ws = await scalar(tx, NEW_WORKSPACE)
    with pytest.raises(IntegrityError, match="data_class"):
        await tx.execute(
            text(
                "INSERT INTO usage_ledger (workspace_id, provider, model, kind)"
                " VALUES (:ws, 'groq', 'm', 'rewrite')"
            ),
            {"ws": ws},
        )


# ---------------------------------------------------------------- A1 across rows
# (security review: rules must hold between parent and child rows, not just within one)

NEW_DOC_CLASSED = (
    "INSERT INTO documents (workspace_id, source_id, external_id, uri, title, mime, checksum,"
    " acl, data_class) VALUES (:ws, :src, gen_random_uuid()::text, 'u', 't', 'text/plain', 'c',"
    " CAST(:acl AS text[]), :dc) RETURNING id"
)
NEW_CHUNK = (
    "INSERT INTO chunks (document_id, workspace_id, ordinal, content, token_count, acl,"
    " data_class) VALUES (:d, :ws, :n, 'text', 1, CAST(:acl AS text[]), :dc) RETURNING id"
)


async def test_gmail_documents_can_never_be_public(tx: AsyncConnection) -> None:
    ws, src = await workspace_and_source(tx, connector="gmail_mbox", visibility="{public}")
    with pytest.raises(IntegrityError, match="documents_match_source"):
        await tx.execute(
            text(NEW_DOC_CLASSED), {"ws": ws, "src": src, "acl": "{public}", "dc": "public"}
        )


async def test_document_class_and_workspace_must_match_source(tx: AsyncConnection) -> None:
    ws, src = await workspace_and_source(tx, data_class="private")
    other_ws = await scalar(tx, NEW_WORKSPACE)
    cases = [
        {"ws": ws, "src": src, "acl": "{public}", "dc": "public"},  # public under private source
        {"ws": other_ws, "src": src, "acl": "{public}", "dc": "private"},  # other workspace
    ]
    for params in cases:
        with pytest.raises(IntegrityError, match="documents_match_source"):
            async with tx.begin_nested():
                await tx.execute(text(NEW_DOC_CLASSED), params)


async def test_chunk_class_and_workspace_must_match_document(tx: AsyncConnection) -> None:
    ws, src = await workspace_and_source(tx)
    doc = await scalar(tx, NEW_DOC_CLASSED, ws=ws, src=src, acl="{public}", dc="private")
    other_ws = await scalar(tx, NEW_WORKSPACE)
    cases = [
        {"ws": ws, "dc": "public"},  # chunk claims public, its document is private
        {"ws": other_ws, "dc": "private"},  # chunk moved to another workspace
    ]
    for n, params in enumerate(cases):
        with pytest.raises(IntegrityError, match="chunks_match_document"):
            async with tx.begin_nested():
                await tx.execute(text(NEW_CHUNK), {"d": doc, "n": n, "acl": "{public}", **params})


async def test_public_chain_is_accepted(tx: AsyncConnection) -> None:
    ws, src = await workspace_and_source(tx, data_class="public")
    doc = await scalar(tx, NEW_DOC_CLASSED, ws=ws, src=src, acl="{public}", dc="public")
    chunk = await scalar(tx, NEW_CHUNK, d=doc, ws=ws, n=0, acl="{public}", dc="public")
    assert chunk is not None


async def test_chunk_acl_is_always_copied_from_its_document(tx: AsyncConnection) -> None:
    ws, src = await workspace_and_source(tx)
    doc = await scalar(tx, NEW_DOC_CLASSED, ws=ws, src=src, acl="{public}", dc="private")
    chunk = await scalar(tx, NEW_CHUNK, d=doc, ws=ws, n=0, acl="{user:attacker}", dc="private")
    get_acl = "SELECT acl FROM chunks WHERE id = :c"
    assert await scalar(tx, get_acl, c=chunk) == ["public"]  # the caller's acl is ignored

    # Narrowing the document's ACL narrows its chunks in the same transaction.
    await tx.execute(text("UPDATE documents SET acl = '{group:eng}' WHERE id = :d"), {"d": doc})
    assert await scalar(tx, get_acl, c=chunk) == ["group:eng"]


async def test_making_a_source_private_cascades_to_documents_and_chunks(
    tx: AsyncConnection,
) -> None:
    ws, src = await workspace_and_source(tx, data_class="public")
    doc = await scalar(tx, NEW_DOC_CLASSED, ws=ws, src=src, acl="{public}", dc="public")
    chunk = await scalar(tx, NEW_CHUNK, d=doc, ws=ws, n=0, acl="{public}", dc="public")
    await tx.execute(text("UPDATE sources SET data_class = 'private' WHERE id = :s"), {"s": src})
    assert await scalar(tx, "SELECT data_class FROM chunks WHERE id = :c", c=chunk) == "private"


async def test_private_chunks_cannot_enter_a_public_scoped_generation(
    tx: AsyncConnection,
) -> None:
    ws, src = await workspace_and_source(tx)
    doc = await scalar(tx, NEW_DOC_CLASSED, ws=ws, src=src, acl="{public}", dc="private")
    chunk = await scalar(tx, NEW_CHUNK, d=doc, ws=ws, n=0, acl="{public}", dc="private")
    gen = await scalar(
        tx,
        "INSERT INTO embedding_generations (model, dims, status, data_scope)"
        " VALUES ('bge-m3', 1024, 'past', 'public') RETURNING id",
    )
    with pytest.raises(IntegrityError, match="public-scoped generation"):
        await tx.execute(
            text(
                "INSERT INTO chunk_embeddings (chunk_id, generation_id, embedding)"
                " VALUES (:c, :g, CAST(array_fill(0.1, ARRAY[1024]) AS halfvec(1024)))"
            ),
            {"c": chunk, "g": gen},
        )


async def test_generation_dims_must_match_the_vector_column(tx: AsyncConnection) -> None:
    with pytest.raises(IntegrityError, match="check"):
        await tx.execute(
            text(
                "INSERT INTO embedding_generations (model, dims, status) VALUES ('x', 768, 'past')"
            )
        )


# ---------------------------------------------------------------- content


async def test_chunk_search_vectors_are_generated(tx: AsyncConnection) -> None:
    ws, src = await workspace_and_source(tx)
    doc = await scalar(tx, NEW_DOCUMENT, ws=ws, src=src, acl="{public}")
    matched = await scalar(
        tx,
        "INSERT INTO chunks (document_id, workspace_id, ordinal, heading_path, content,"
        " token_count, acl) VALUES (:d, :ws, 0, 'Deploy', 'Running migrations with alembic',"
        " 4, '{public}') RETURNING tsv_en @@ websearch_to_tsquery('english', 'migration')"
        " AND tsv_simple @@ websearch_to_tsquery('simple', 'alembic')",
        d=doc,
        ws=ws,
    )
    assert matched is True


# ---------------------------------------------------------------- A3: generations


async def _retire_existing_generations(tx: AsyncConnection) -> None:
    # The shared test branch may already hold real generations from manual runs.
    # Retiring them inside the test transaction (rolled back afterwards) keeps
    # these tests independent of branch state.
    await tx.execute(text("UPDATE embedding_generations SET status='past' WHERE status<>'past'"))


async def test_only_one_present_and_one_future_generation(tx: AsyncConnection) -> None:
    await _retire_existing_generations(tx)
    for model, status in [("a", "present"), ("b", "future"), ("c", "past"), ("d", "past")]:
        await tx.execute(text(NEW_GENERATION), {"m": model, "s": status})
    for status in ("present", "future"):
        with pytest.raises(IntegrityError, match=f"embedding_generations_one_{status}"):
            async with tx.begin_nested():
                await tx.execute(text(NEW_GENERATION), {"m": "x", "s": status})


async def test_generation_flip_in_one_transaction(tx: AsyncConnection) -> None:
    await _retire_existing_generations(tx)
    await tx.execute(text(NEW_GENERATION), {"m": "old", "s": "present"})
    new = await scalar(tx, NEW_GENERATION + " RETURNING id", m="new", s="future")
    # Demote first, then promote: the unique index never sees two 'present' rows,
    # and readers outside this transaction see the switch atomically at commit.
    await tx.execute(text("UPDATE embedding_generations SET status='past' WHERE status='present'"))
    await tx.execute(
        text("UPDATE embedding_generations SET status='present' WHERE id=:id"), {"id": new}
    )
    present = "SELECT model FROM embedding_generations WHERE status = 'present'"
    assert await scalar(tx, present) == "new"


# ---------------------------------------------------------------- A2: CHECK constraints


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO jobs (workspace_id, kind, idempotency_key, status)"
        " VALUES (:ws, 'k', gen_random_uuid()::text, 'exploded')",
        "INSERT INTO memberships (workspace_id, user_id, role) VALUES (:ws, :user, 'superuser')",
        "INSERT INTO sources (workspace_id, connector, name, visibility)"
        " VALUES (:ws, 'slack', 's', '{public}')",
        "INSERT INTO jobs (workspace_id, kind, idempotency_key, status)"
        " VALUES (:ws, 'k', gen_random_uuid()::text, 'running')",  # running needs locked_by
    ],
    ids=["job-status", "role", "connector", "running-unlocked"],
)
async def test_check_constraints_reject_invalid_values(tx: AsyncConnection, sql: str) -> None:
    ws = await scalar(tx, NEW_WORKSPACE)
    user = await scalar(
        tx,
        "INSERT INTO users (email, email_canonical, name, password_hash)"
        " SELECT e, e, 'u', 'h' FROM (SELECT 'u' || gen_random_uuid() || '@x.dev' AS e) x"
        " RETURNING id",
    )
    with pytest.raises(IntegrityError, match="check"):
        await tx.execute(text(sql), {"ws": ws, "user": user})


async def test_job_idempotency_key_is_unique(tx: AsyncConnection) -> None:
    ws = await scalar(tx, NEW_WORKSPACE)
    insert = "INSERT INTO jobs (workspace_id, kind, idempotency_key) VALUES (:ws, 'k', 'same')"
    await tx.execute(text(insert), {"ws": ws})
    with pytest.raises(IntegrityError, match="idempotency_key"):
        await tx.execute(text(insert), {"ws": ws})


async def test_every_user_needs_a_password_and_a_canonical_email(tx: AsyncConnection) -> None:
    # 0004 (ADR-0021): no guest; every account signs in with a password.
    with pytest.raises(IntegrityError, match="password_hash"):
        await tx.execute(
            text(
                "INSERT INTO users (email, email_canonical, name)"
                " SELECT e, e, 'n' FROM (SELECT 'n-' || gen_random_uuid() || '@x.dev' AS e) x"
            )
        )


async def test_one_inbox_one_account(tx: AsyncConnection) -> None:
    insert = (
        "INSERT INTO users (email, email_canonical, name, password_hash) VALUES (:e, :c, 'n', 'h')"
    )
    canonical = f"dup-{uuid4()}@gmail.com"
    await tx.execute(text(insert), {"e": canonical, "c": canonical})
    with pytest.raises(IntegrityError, match="users_email_canonical_key"):
        await tx.execute(text(insert), {"e": "x." + canonical, "c": canonical.upper()})


async def test_a_user_belongs_to_one_workspace_until_invites_exist(tx: AsyncConnection) -> None:
    user = await scalar(
        tx,
        "INSERT INTO users (email, email_canonical, name, password_hash)"
        " SELECT e, e, 'u', 'h' FROM (SELECT 'm' || gen_random_uuid() || '@x.dev' AS e) x"
        " RETURNING id",
    )
    first, second = await scalar(tx, NEW_WORKSPACE), await scalar(tx, NEW_WORKSPACE)
    member = "INSERT INTO memberships (workspace_id, user_id, role) VALUES (:w, :u, 'owner')"
    await tx.execute(text(member), {"w": first, "u": user})
    with pytest.raises(IntegrityError, match="memberships_one_per_user"):
        await tx.execute(text(member), {"w": second, "u": user})


async def test_email_codes_hold_only_32_byte_hashes(tx: AsyncConnection) -> None:
    user = await scalar(
        tx,
        "INSERT INTO users (email, email_canonical, name, password_hash)"
        " SELECT e, e, 'u', 'h' FROM (SELECT 'c' || gen_random_uuid() || '@x.dev' AS e) x"
        " RETURNING id",
    )
    with pytest.raises(IntegrityError, match="email_codes_code_hash_check"):
        await tx.execute(
            text(
                "INSERT INTO email_codes (user_id, purpose, code_hash, expires_at)"
                " VALUES (:u, 'verify_email', '\x0102', now())"
            ),
            {"u": user},
        )
