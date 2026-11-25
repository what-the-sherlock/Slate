"""The ingestion pipeline against real Neon Postgres + storage, with a FAKE embedder
(1024-d, deterministic): exercises halfvec inserts, the HNSW index, the generation
registry, the usage ledger and the one-transaction write, at zero model cost.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from core.adapters.factories import s3_blob_store
from core.adapters.parsers.formats import FormatParser
from core.adapters.postgres.documents import PostgresDocumentStore
from core.adapters.postgres.sources import PostgresSourceStore
from core.adapters.postgres.uow import UnitOfWork
from core.adapters.postgres.usage import PostgresGenerationStore, PostgresUsageLedger
from core.adapters.s3.blob_store import S3BlobStore
from core.config import Settings
from core.domain.budget import SpendLimits
from core.domain.data_class import DataClass
from core.domain.errors import ValidationFailed
from core.domain.ids import JobId, WorkspaceId
from core.domain.jobs import Job, JobStatus
from core.domain.principals import Principals, user_principal
from core.domain.uploads import MIME, FileKind
from core.services.ingest import IngestService
from tests.fakes.models import FakeEmbedder, WordTokenizer
from tests.fakes.system import FakeClock

MD = b"# Handbook\n\n## Leave\n\nEveryone gets 25 days of paid leave per year.\n"


@pytest.fixture
def blobs(settings: Settings) -> S3BlobStore:
    store = s3_blob_store(settings)
    if store is None:
        pytest.skip("AWS_* storage settings not configured")
    return store


@pytest.fixture
async def world(uow: UnitOfWork) -> AsyncIterator[tuple[WorkspaceId, UUID]]:
    async with uow() as s:
        present = (
            await s.execute(
                text("SELECT count(*) FROM embedding_generations WHERE status='present'")
            )
        ).scalar_one()
        if present:
            pytest.skip("a 'present' generation already exists on this branch")
        ws = (
            await s.execute(
                text(
                    "INSERT INTO workspaces (name, slug) VALUES ('ing', 'ing-' || gen_random_uuid()) RETURNING id"
                )
            )
        ).scalar_one()
        user = (
            await s.execute(
                text(
                    "INSERT INTO users (email, email_canonical, name, password_hash)"
                    " SELECT e, e, 'I', 'h' FROM (SELECT 'ing-' || gen_random_uuid() || '@t.dev' AS e) x"
                    " RETURNING id"
                )
            )
        ).scalar_one()
    yield WorkspaceId(UUID(str(ws))), UUID(str(user))
    async with uow() as s:
        gens = (
            (
                await s.execute(
                    text("SELECT id FROM embedding_generations WHERE model = 'fake-embed'")
                )
            )
            .scalars()
            .all()
        )
        for gen in gens:
            await s.execute(text(f"DROP INDEX IF EXISTS chunk_embeddings_hnsw_g{int(gen)}"))
        await s.execute(text("DELETE FROM workspaces WHERE id = :w"), {"w": ws})
        await s.execute(text("DELETE FROM embedding_generations WHERE model = 'fake-embed'"))
        await s.execute(text("DELETE FROM users WHERE id = :u"), {"u": user})


def service(uow: UnitOfWork, blobs: S3BlobStore) -> IngestService:
    return IngestService(
        blobs,
        PostgresDocumentStore(uow),
        PostgresSourceStore(uow),
        FormatParser(),
        WordTokenizer(),
        FakeEmbedder(dims=1024),
        PostgresGenerationStore(uow),
        PostgresUsageLedger(uow),
        limits=SpendLimits.monthly_only(Decimal(5)),
    )


async def make_job(
    uow: UnitOfWork,
    blobs: S3BlobStore,
    ws: WorkspaceId,
    user: UUID,
    data: bytes,
    stored: bytes | None = None,
) -> Job:
    source = await PostgresSourceStore(uow).upload_source(ws, DataClass.PRIVATE)
    digest = hashlib.sha256(data).hexdigest()
    key = f"uploads/{ws}/{user}/{digest}.md"
    await blobs.put(key, data if stored is None else stored, MIME[FileKind.MARKDOWN])
    return Job(
        id=JobId(uuid4()),
        workspace_id=ws,
        kind="process_upload",
        payload={
            "blob_key": key,
            "filename": "handbook.md",
            "kind": "md",
            "mime": MIME[FileKind.MARKDOWN],
            "size": len(data),
            "sha256": digest,
            "acl": [f"user:{user}"],
            "data_class": "private",
            "source_id": str(source),
            "external_id": f"upload:{user}:{digest}",
            "uploader": str(user),
        },
        status=JobStatus.RUNNING,
        attempts=1,
        max_attempts=5,
        run_after=FakeClock().now(),
        idempotency_key=f"k-{uuid4()}",
    )


async def test_pipeline_writes_document_chunks_and_vectors_in_postgres(
    uow: UnitOfWork, blobs: S3BlobStore, world: tuple[WorkspaceId, UUID]
) -> None:
    ws, user = world
    job = await make_job(uow, blobs, ws, user, MD + uuid4().hex.encode())
    try:
        await service(uow, blobs).process_upload(job)
        async with uow() as s:
            row = (
                await s.execute(
                    text(
                        "SELECT count(DISTINCT c.id), count(e.chunk_id), min(c.acl::text),"
                        " min(c.heading_path) FROM documents d JOIN chunks c ON c.document_id = d.id"
                        " LEFT JOIN chunk_embeddings e ON e.chunk_id = c.id WHERE d.workspace_id = :ws"
                    ),
                    {"ws": ws},
                )
            ).one()
            run = (
                await s.execute(
                    text(
                        "SELECT status, docs_changed FROM ingestion_runs WHERE workspace_id = :ws"
                    ),
                    {"ws": ws},
                )
            ).one()
            ledger = (
                await s.execute(
                    text("SELECT kind, data_class FROM usage_ledger WHERE workspace_id = :ws"),
                    {"ws": ws},
                )
            ).one()
            index = (
                await s.execute(
                    text(
                        "SELECT count(*) FROM pg_indexes WHERE indexname LIKE 'chunk_embeddings_hnsw_g%'"
                    )
                )
            ).scalar_one()
        chunks, vectors, acl, heading = row
        assert chunks >= 1
        assert vectors == chunks  # every chunk has its embedding (one transaction)
        assert acl == f"{{user:{user}}}"  # copied from the document by trigger
        assert heading == "Handbook > Leave"
        assert tuple(run) == ("succeeded", 1)
        assert tuple(ledger) == ("embed", "private")
        assert index >= 1

        # ACL enforced in SQL: the owner sees it, another user doesn't.
        store = PostgresDocumentStore(uow)
        async with uow() as s:
            doc_id = (
                await s.execute(
                    text("SELECT id FROM documents WHERE workspace_id = :ws"), {"ws": ws}
                )
            ).scalar_one()
        assert await store.get_document(ws, doc_id, Principals((user_principal(user),)))  # type: ignore[arg-type]
        from core.domain.errors import NotFound

        with pytest.raises(NotFound):
            await store.get_document(ws, doc_id, Principals((f"user:{uuid4()}",)))
    finally:
        await blobs.delete(str(job.payload["blob_key"]))


async def test_swapped_object_never_reaches_the_database(
    uow: UnitOfWork, blobs: S3BlobStore, world: tuple[WorkspaceId, UUID]
) -> None:
    ws, user = world
    job = await make_job(uow, blobs, ws, user, MD, stored=b"# Injected\n\nnot the announced file")
    try:
        with pytest.raises(ValidationFailed, match="doesn't match"):
            await service(uow, blobs).process_upload(job)
        async with uow() as s:
            docs = (
                await s.execute(
                    text("SELECT count(*) FROM documents WHERE workspace_id = :ws"), {"ws": ws}
                )
            ).scalar_one()
            err = (
                await s.execute(
                    text(
                        "SELECT e.stage FROM ingestion_errors e JOIN ingestion_runs r ON r.id = e.run_id"
                        " WHERE r.workspace_id = :ws"
                    ),
                    {"ws": ws},
                )
            ).scalar_one()
        assert docs == 0
        assert err == "parse"
    finally:
        await blobs.delete(str(job.payload["blob_key"]))
