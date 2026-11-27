"""Migration 0012 (P1-3) on a real Neon test branch, in rolled-back transactions."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
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


async def _chunk(conn: AsyncConnection, title: str, body: str, context: str) -> int:
    ws = (
        await conn.execute(
            text(
                "INSERT INTO workspaces (name, slug) VALUES ('s', 's-' || gen_random_uuid())"
                " RETURNING id"
            )
        )
    ).scalar_one()
    src = (
        await conn.execute(
            text(
                "INSERT INTO sources (workspace_id, connector, name, visibility)"
                " VALUES (:w, 'upload', 'Uploads', ARRAY['user:x']) RETURNING id"
            ),
            {"w": ws},
        )
    ).scalar_one()
    doc = (
        await conn.execute(
            text(
                "INSERT INTO documents (workspace_id, source_id, external_id, uri, title, mime,"
                " checksum, acl) VALUES (:w, :s, gen_random_uuid()::text, 'u', :t, 'text/plain',"
                " 'c', ARRAY['user:x']) RETURNING id"
            ),
            {"w": ws, "s": src, "t": title},
        )
    ).scalar_one()
    return int(
        (
            await conn.execute(
                text(
                    "INSERT INTO chunks (document_id, workspace_id, ordinal, content, token_count,"
                    " acl, search_context) VALUES (:d, :w, 0, :b, 1, '{}', :c) RETURNING id"
                ),
                {"d": doc, "w": ws, "b": body, "c": context},
            )
        ).scalar_one()
    )


async def _rank(conn: AsyncConnection, chunk: int, query: str) -> float | None:
    row = (
        await conn.execute(
            text(
                "SELECT ts_rank_cd(tsv_en, websearch_to_tsquery('english', :q)) AS r,"
                " tsv_en @@ websearch_to_tsquery('english', :q) AS hit FROM chunks WHERE id = :c"
            ),
            {"q": query, "c": chunk},
        )
    ).one()
    return float(row.r) if row.hit else None


async def test_title_words_are_keyword_searchable(tx: AsyncConnection) -> None:
    chunk = await _chunk(tx, "Brand guidelines", "Use the navy logo.", "Brand guidelines")
    assert await _rank(tx, chunk, "brand guidelines") is not None  # only in the title
    simple = (
        await tx.execute(
            text("SELECT tsv_simple @@ to_tsquery('simple', 'brand') FROM chunks WHERE id = :c"),
            {"c": chunk},
        )
    ).scalar_one()
    assert simple is True


async def test_a_title_match_outranks_a_passing_mention(tx: AsyncConnection) -> None:
    titled = await _chunk(tx, "Onboarding", "Day one: laptop and accounts.", "Onboarding")
    mention = await _chunk(tx, "Misc", "We talked about onboarding once.", "Misc")
    title_rank, body_rank = (
        await _rank(tx, titled, "onboarding"),
        await _rank(tx, mention, "onboarding"),
    )
    assert title_rank is not None
    assert body_rank is not None
    assert title_rank > body_rank


async def test_search_context_defaults_to_empty(tx: AsyncConnection) -> None:
    default = (
        await tx.execute(
            text(
                "SELECT column_default, is_nullable FROM information_schema.columns"
                " WHERE table_name = 'chunks' AND column_name = 'search_context'"
            )
        )
    ).one()
    assert default.is_nullable == "NO"
    assert "''" in str(default.column_default)
