"""Migration 0011 (P0-4) on a real Neon test branch, in rolled-back transactions."""

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


async def _source(conn: AsyncConnection) -> object:
    ws = (
        await conn.execute(
            text(
                "INSERT INTO workspaces (name, slug) VALUES ('s', 's-' || gen_random_uuid())"
                " RETURNING id"
            )
        )
    ).scalar_one()
    return (
        await conn.execute(
            text(
                "INSERT INTO sources (workspace_id, connector, name, visibility)"
                " VALUES (:w, 'gmail_mbox', 'Email import', ARRAY['user:x']) RETURNING id"
            ),
            {"w": ws},
        )
    ).scalar_one()


async def _run(conn: AsyncConnection, source: object, kind: str) -> object:
    return (
        await conn.execute(
            text(
                "INSERT INTO ingestion_runs (workspace_id, source_id, kind)"
                " SELECT workspace_id, id, :k FROM sources WHERE id = :s RETURNING id"
            ),
            {"s": source, "k": kind},
        )
    ).scalar_one()


async def test_import_runs_are_allowed_and_skips_default_to_empty(tx: AsyncConnection) -> None:
    run = await _run(tx, await _source(tx), "import")  # rejected before 0011
    row = (
        await tx.execute(
            text("SELECT docs_skipped, skipped FROM ingestion_runs WHERE id = :r"), {"r": run}
        )
    ).one()
    assert (row.docs_skipped, row.skipped) == (0, {})


async def test_skips_must_be_an_object_and_non_negative(tx: AsyncConnection) -> None:
    run = await _run(tx, await _source(tx), "full")
    await tx.execute(
        text(
            "UPDATE ingestion_runs SET docs_skipped = 2,"
            " skipped = '{\"unsupported_type:pdf\": 2}' WHERE id = :r"
        ),
        {"r": run},
    )
    for bad in ("skipped = '[]'", "docs_skipped = -1"):
        nested = await tx.begin_nested()
        with pytest.raises(DBAPIError):
            await tx.execute(text(f"UPDATE ingestion_runs SET {bad} WHERE id = :r"), {"r": run})  # noqa: S608
        await nested.rollback()


async def test_unknown_run_kinds_are_still_rejected(tx: AsyncConnection) -> None:
    source = await _source(tx)
    with pytest.raises(DBAPIError):
        await _run(tx, source, "nonsense")
