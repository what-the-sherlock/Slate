"""Migration 0013 (P1-4) on a real Neon test branch, in rolled-back transactions."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

pytestmark = pytest.mark.integration

INSERT = (
    "INSERT INTO usage_ledger (workspace_id, provider, model, kind, data_class, tokens_in,"
    " tokens_out, cost_usd) SELECT id, 'openai', 'gpt-4o-mini', :k, 'private', 1, 1, 0"
    " FROM workspaces LIMIT 1"
)


@pytest.fixture
async def tx(direct_engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    async with direct_engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(
                text("INSERT INTO workspaces (name, slug) VALUES ('s', 's-' || gen_random_uuid())")
            )
            yield conn
        finally:
            await trans.rollback()


async def test_rerank_spend_can_be_recorded(tx: AsyncConnection) -> None:
    await tx.execute(text(INSERT), {"k": "rerank"})


async def test_unknown_usage_kinds_are_still_rejected(tx: AsyncConnection) -> None:
    with pytest.raises(DBAPIError):
        await tx.execute(text(INSERT), {"k": "nonsense"})
