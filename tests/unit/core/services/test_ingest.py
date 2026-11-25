from __future__ import annotations

import hashlib
from decimal import Decimal
from uuid import uuid4

import pytest

from core.adapters.parsers.formats import FormatParser
from core.domain.budget import SpendLimits
from core.domain.documents import SearchFilters
from core.domain.errors import BudgetExceeded, ValidationFailed
from core.domain.ids import JobId, SourceId, UserId, WorkspaceId
from core.domain.jobs import Job, JobStatus
from core.domain.principals import PUBLIC, Principals, user_principal
from core.domain.uploads import MAX_UPLOAD_BYTES, MIME, FileKind
from core.services.ingest import IngestService
from tests.fakes.models import FakeEmbedder, WordTokenizer
from tests.fakes.sources import InMemorySources
from tests.fakes.storage import InMemoryBlobStore, InMemoryIndex
from tests.fakes.system import FakeClock
from tests.fakes.usage import InMemoryGenerations, InMemoryLedger
from tests.fixtures.documents import minimal_pdf

WS = WorkspaceId(uuid4())
UPLOADER = UserId(uuid4())
SOURCE = SourceId(uuid4())
MD = b"# Handbook\n\n## Leave\n\nEveryone gets 25 days of paid leave per year.\n"


class Env:
    def __init__(self, *, spent: Decimal = Decimal(0)) -> None:
        self.blobs = InMemoryBlobStore()
        self.index = InMemoryIndex()
        self.sources = InMemorySources()
        self.embedder = FakeEmbedder(dims=16)
        self.ledger = InMemoryLedger(spent)
        self.svc = IngestService(
            self.blobs,
            self.index,
            self.sources,
            FormatParser(),
            WordTokenizer(),
            self.embedder,
            InMemoryGenerations(),
            self.ledger,
            limits=SpendLimits.monthly_only(Decimal(5)),
        )

    async def job(
        self,
        data: bytes,
        kind: FileKind = FileKind.MARKDOWN,
        *,
        stored: bytes | None = None,
        acl: tuple[str, ...] = (f"user:{UPLOADER}",),
        data_class: str = "private",
    ) -> Job:
        key = f"uploads/{WS}/{UPLOADER}/{hashlib.sha256(data).hexdigest()}.{kind.value}"
        await self.blobs.put(key, data if stored is None else stored, MIME[kind])
        return Job(
            id=JobId(uuid4()),
            workspace_id=WS,
            kind="process_upload",
            payload={
                "blob_key": key,
                "filename": f"handbook.{kind.value}",
                "kind": kind.value,
                "mime": MIME[kind],
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "acl": list(acl),
                "data_class": data_class,
                "source_id": str(SOURCE),
                "external_id": f"upload:{UPLOADER}:{hashlib.sha256(data).hexdigest()}",
                "uploader": str(UPLOADER),
            },
            status=JobStatus.RUNNING,
            attempts=1,
            max_attempts=5,
            run_after=FakeClock().now(),
            idempotency_key="k",
        )

    def run_status(self) -> str:
        assert len(self.sources.finished) == 1
        return next(iter(self.sources.finished.values()))[0]


@pytest.fixture
def env() -> Env:
    return Env()


async def test_upload_is_parsed_chunked_embedded_and_searchable(env: Env) -> None:
    await env.svc.process_upload(await env.job(MD))
    assert env.run_status() == "succeeded"
    owner = Principals((user_principal(UPLOADER),))
    hits = await env.index.keyword_search(WS, owner, "leave", 10, SearchFilters())
    assert hits
    assert hits[0].heading_path == "Handbook > Leave"
    entry = env.ledger.entries[0]
    assert (entry.kind, entry.data_class, entry.provider) == ("embed", "private", "openai")


async def test_private_upload_is_invisible_to_a_guest(env: Env) -> None:
    await env.svc.process_upload(await env.job(MD))
    guest = Principals((f"user:{uuid4()}", PUBLIC))
    assert await env.index.keyword_search(WS, guest, "leave", 10, SearchFilters()) == []


async def test_pdf_end_to_end(env: Env) -> None:
    pdf = minimal_pdf(["Expense policy", "Receipts within 30 days"], title="Expenses")
    await env.svc.process_upload(await env.job(pdf, FileKind.PDF))
    assert env.run_status() == "succeeded"


async def test_already_indexed_bytes_skip_the_paid_embedding_call(env: Env) -> None:
    job = await env.job(MD)
    await env.svc.process_upload(job)
    calls = len(env.embedder.calls)
    # The sources store now knows the upload (as the Postgres store would after the write).
    from core.domain.data_class import DataClass

    env.sources.index(WS, str(job.payload["external_id"]), (f"user:{UPLOADER}",), DataClass.PRIVATE)
    env.sources.finished.clear()
    await env.svc.process_upload(job)
    assert env.run_status() == "succeeded"
    assert len(env.embedder.calls) == calls  # no second embedding call, no cost


# ---------------------------------------------------------------- the security gate


async def test_object_swapped_after_complete_is_rejected(env: Env) -> None:
    # The presigned form was reused to replace the object with different bytes.
    job = await env.job(MD, stored=b"# Different\n\nInjected content " + b"x" * 40)
    with pytest.raises(ValidationFailed, match="doesn't match"):
        await env.svc.process_upload(job)
    assert env.run_status() == "failed"
    assert env.sources.errors[0][1] == "parse"
    assert env.embedder.calls == []  # nothing was sent to the embedder


async def test_swapped_bytes_with_matching_hash_claim_but_wrong_type(env: Env) -> None:
    exe = b"MZ\x90\x00 not a pdf at all"
    with pytest.raises(ValidationFailed, match=r"valid \.pdf"):
        await env.svc.process_upload(await env.job(exe, FileKind.PDF))


async def test_oversized_object_is_not_downloaded(env: Env) -> None:
    job = await env.job(MD)
    key = str(job.payload["blob_key"])
    env.blobs.blobs[key] = (b"x" * (MAX_UPLOAD_BYTES + 1), "text/markdown")
    with pytest.raises(ValidationFailed, match="larger than 25 MB"):
        await env.svc.process_upload(job)


async def test_file_with_no_text_fails_cleanly(env: Env) -> None:
    with pytest.raises(ValidationFailed, match="No text"):
        await env.svc.process_upload(await env.job(b"\n\n   \n"))
    assert env.run_status() == "failed"


async def test_budget_exhausted_stops_before_any_model_call() -> None:
    env = Env(spent=Decimal(5))
    with pytest.raises(BudgetExceeded):
        await env.svc.process_upload(await env.job(MD))
    assert env.embedder.calls == []
    assert env.run_status() == "failed"


async def test_a_workspace_over_its_allowance_cannot_embed_but_others_can() -> None:
    env = Env()
    env.svc._limits = SpendLimits(monthly_all=Decimal(5), monthly_workspace=Decimal("0.25"))
    env.ledger.workspace_spent[WS] = Decimal("0.25")
    with pytest.raises(BudgetExceeded, match="this month's AI allowance"):
        await env.svc.process_upload(await env.job(MD))
    assert env.embedder.calls == []
