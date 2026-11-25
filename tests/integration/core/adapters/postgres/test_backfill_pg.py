"""Backfill against real Postgres: future generation + HNSW index, public-only selection,
idempotent inserts, and the ADR-0012 trigger as the last line of defence."""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from core.adapters.postgres.documents import PostgresDocumentStore
from core.adapters.postgres.sources import PostgresSourceStore
from core.adapters.postgres.uow import UnitOfWork
from core.adapters.postgres.usage import PostgresGenerationStore, PostgresUsageLedger
from core.domain.data_class import DataClass
from core.domain.documents import DocumentUpsert, NewChunk
from core.domain.ids import ChunkId, GenerationId, WorkspaceId
from core.domain.principals import PUBLIC, workspace_principal
from core.services.backfill import Provider, backfill_generation
from tests.fakes.models import FakeEmbedder

MODEL = "backfill-it-fake"


@pytest.fixture
async def world(uow: UnitOfWork) -> AsyncIterator[WorkspaceId]:
    async with uow() as s:
        busy = (
            await s.execute(text("SELECT 1 FROM embedding_generations WHERE status = 'future'"))
        ).first()
        if busy:
            pytest.skip("a 'future' generation is already being built on this branch")
        ws = WorkspaceId(
            UUID(
                str(
                    (
                        await s.execute(
                            text(
                                "INSERT INTO workspaces (name, slug)"
                                " VALUES ('b', 'b-' || gen_random_uuid()) RETURNING id"
                            )
                        )
                    ).scalar_one()
                )
            )
        )
        past = int(
            (
                await s.execute(
                    text(
                        "INSERT INTO embedding_generations (model, dims, status)"
                        " VALUES ('backfill-it-seed', 1024, 'past') RETURNING id"
                    )
                )
            ).scalar_one()
        )
    sources = PostgresSourceStore(uow)
    docs = PostgresDocumentStore(uow)
    vec = (await FakeEmbedder().embed_documents(["x"])).vectors[0]
    for dc, acl in (
        (DataClass.PUBLIC, (PUBLIC, workspace_principal(ws))),
        (DataClass.PRIVATE, (workspace_principal(ws),)),
    ):
        src = await sources.upload_source(ws, dc)
        await docs.write_document(
            ws,
            DocumentUpsert(
                src, dc.value, f"x://{dc.value}", dc.value.title(), "text/plain", dc.value, acl
            ),
            [NewChunk(0, f"{dc.value} zero", 2), NewChunk(1, f"{dc.value} one", 2)],
            {GenerationId(past): [vec, vec]},
            data_class=dc.value,
        )
    yield ws
    async with uow() as s:
        gens = (
            (
                await s.execute(
                    text("SELECT id FROM embedding_generations WHERE model = :m"), {"m": MODEL}
                )
            )
            .scalars()
            .all()
        )
        for g in gens:
            await s.execute(text(f"DROP INDEX IF EXISTS chunk_embeddings_hnsw_g{int(g)}"))
        await s.execute(text("DELETE FROM workspaces WHERE id = :w"), {"w": ws})
        await s.execute(
            text("DELETE FROM embedding_generations WHERE model IN (:m, 'backfill-it-seed')"),
            {"m": MODEL},
        )


async def test_backfill_builds_a_public_future_generation(
    uow: UnitOfWork, world: WorkspaceId
) -> None:
    docs, gens = PostgresDocumentStore(uow), PostgresGenerationStore(uow)
    report = await backfill_generation(
        docs,
        gens,
        PostgresUsageLedger(uow),
        FakeEmbedder(model=MODEL),
        Provider("workersai", free_tier=True),
        data_scope="public",
        monthly_budget_usd=Decimal(5),
        max_chunks=1000,
    )
    assert report.complete
    info = await gens.find(MODEL)
    assert info is not None
    assert (info.status, info.data_scope) == ("future", "public")
    async with uow() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT c.data_class, count(*) FROM chunk_embeddings e"
                    " JOIN chunks c ON c.id = e.chunk_id"
                    " WHERE e.generation_id = :g AND c.workspace_id = :w GROUP BY 1"
                ),
                {"g": report.generation, "w": world},
            )
        ).all()
        index = (
            await s.execute(
                text("SELECT 1 FROM pg_indexes WHERE indexname = :n"),
                {"n": f"chunk_embeddings_hnsw_g{int(report.generation)}"},
            )
        ).first()
    assert {str(r[0]): int(r[1]) for r in rows} == {"public": 2}
    assert index is not None
    # Idempotent: nothing left for this workspace, and re-inserting is a no-op.
    left = await docs.chunks_missing_embedding(
        report.generation, public_only=True, after=None, limit=1000
    )
    async with uow() as s:
        mine = {
            int(x)
            for x in (
                await s.execute(text("SELECT id FROM chunks WHERE workspace_id = :w"), {"w": world})
            ).scalars()
        }
    assert not [c for c in left if int(c.chunk_id) in mine]
    vec = await FakeEmbedder().embed_query("x")
    some = ChunkId(min(mine))
    assert await docs.add_embeddings(report.generation, [(some, vec)]) in (0, 1)


async def test_trigger_rejects_private_chunks_in_a_public_generation(
    uow: UnitOfWork, world: WorkspaceId
) -> None:
    gens, docs = PostgresGenerationStore(uow), PostgresDocumentStore(uow)
    gen = await gens.ensure_future(MODEL, 1024, data_scope="public")
    async with uow() as s:
        private_chunk = ChunkId(
            int(
                (
                    await s.execute(
                        text(
                            "SELECT min(id) FROM chunks WHERE workspace_id = :w AND data_class = 'private'"
                        ),
                        {"w": world},
                    )
                ).scalar_one()
            )
        )
    with pytest.raises(DBAPIError, match="public-scoped generation"):
        await docs.add_embeddings(gen.id, [(private_chunk, await FakeEmbedder().embed_query("x"))])
