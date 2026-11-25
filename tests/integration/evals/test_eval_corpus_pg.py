"""The real eval corpus and questions against real Postgres, at zero cost.

Documents are chunked exactly like ingestion and stored with FAKE vectors under a
throwaway 'past' generation, in a throwaway workspace. Vector scores are meaningless
here; what this proves is the plumbing (loading, id mapping, both lanes, scoring)
and that the guest never receives a private document from either lane.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

import pytest
from sqlalchemy import text

from core.adapters.parsers.formats import FormatParser
from core.adapters.postgres.documents import PostgresDocumentStore
from core.adapters.postgres.evals import PostgresEvalStore
from core.adapters.postgres.search import PostgresSearchIndex
from core.adapters.postgres.sources import PostgresSourceStore
from core.adapters.postgres.uow import UnitOfWork
from core.adapters.tokenizer import TiktokenTokenizer
from core.domain.chunking import chunk_sections, embedding_text
from core.domain.data_class import DataClass
from core.domain.documents import DocumentUpsert
from core.domain.ids import GenerationId, WorkspaceId
from core.domain.principals import PUBLIC, workspace_principal
from core.services.search import VectorLane
from evals.cases import EXTERNAL_PREFIX, load_cases, load_corpus
from evals.harness import run_cases
from tests.fakes.models import FakeEmbedder

SEED_MODEL = "eval-it-fake"


@pytest.fixture
async def loaded(uow: UnitOfWork) -> AsyncIterator[tuple[WorkspaceId, GenerationId]]:
    store = PostgresEvalStore(uow)
    ws = await store.ensure_workspace(f"eval-it-{UUID(int=0).hex[:4]}-x", "eval it")
    async with uow() as s:
        await s.execute(text("DELETE FROM documents WHERE workspace_id = :w"), {"w": ws})
        gen = GenerationId(
            int(
                (
                    await s.execute(
                        text(
                            "INSERT INTO embedding_generations (model, dims, status)"
                            " VALUES (:m, 1024, 'past') RETURNING id"
                        ),
                        {"m": SEED_MODEL},
                    )
                ).scalar_one()
            )
        )
    sources, docs, parser, tok, emb = (
        PostgresSourceStore(uow),
        PostgresDocumentStore(uow),
        FormatParser(),
        TiktokenTokenizer(),
        FakeEmbedder(),
    )
    for d in load_corpus():
        public = d.visibility == "public"
        dc = DataClass.PUBLIC if public else DataClass.PRIVATE
        chunks = chunk_sections(parser.parse(d.body, "text/markdown").sections, tok.count)
        vectors = (await emb.embed_documents([embedding_text(d.title, c) for c in chunks])).vectors
        await docs.write_document(
            ws,
            DocumentUpsert(
                await sources.upload_source(ws, dc),
                d.external_id,
                d.external_id,
                d.title,
                "text/markdown",
                d.checksum,
                (PUBLIC, workspace_principal(ws)) if public else (workspace_principal(ws),),
            ),
            chunks,
            {gen: vectors},
            data_class=dc.value,
        )
    yield ws, gen
    async with uow() as s:
        await s.execute(text("DELETE FROM workspaces WHERE id = :w"), {"w": ws})
        await s.execute(text("DELETE FROM embedding_generations WHERE id = :g"), {"g": gen})


async def test_eval_suite_runs_and_never_leaks(
    uow: UnitOfWork, loaded: tuple[WorkspaceId, GenerationId]
) -> None:
    ws, gen = loaded
    store = PostgresEvalStore(uow)
    corpus = load_corpus()
    ids = await store.document_ids(ws, [d.external_id for d in corpus])
    assert len(ids) == len(corpus)
    doc_ids = {k.removeprefix(EXTERNAL_PREFIX): v for k, v in ids.items()}
    private = await store.private_document_ids(ws)
    assert len(private) == sum(d.visibility == "private" for d in corpus)

    for vector in (None, VectorLane(FakeEmbedder(), gen)):  # corpus loaded once for both
        report = await run_cases(
            PostgresSearchIndex(uow), ws, load_cases(), doc_ids, private, vector=vector
        )
        assert report.summary["isolation"] == {"guest_leaks": 0, "cases_with_leaks": []}
        by_tag = report.summary["by_tag"]
        assert isinstance(by_tag, dict)
        print("keyword-only" if vector is None else "fake vectors", report.summary["overall"])
        for c in report.cases:
            if c.recall is not None and c.recall < 1:
                print("  miss", c.id, c.recall, c.top_documents[:3])
        # Exact identifiers must be found by the keyword lane alone.
        assert by_tag["keyword"]["recall@10"] >= 0.9
