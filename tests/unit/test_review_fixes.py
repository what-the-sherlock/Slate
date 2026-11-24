"""Regression tests for the third security review (slices 4-6). One test per finding."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

import pytest

import core.adapters.parsers.formats as formats
import core.services.ingest as ingest_module
import core.services.inline as inline_module
from core.adapters.parsers.formats import FormatParser
from core.domain.auth import Identity, Role
from core.domain.chunking import ChunkingConfig, chunk_sections
from core.domain.documents import Section
from core.domain.errors import BudgetExceeded, NotFound, ValidationFailed
from core.domain.ids import RunId, SessionId, SourceId, UserId, WorkspaceId
from core.domain.jobs import Job, JobStatus
from core.domain.principals import Principals
from core.domain.uploads import MIME, FileKind
from core.ports.catalog import RunSummary, SourceSummary
from core.services.catalog import CatalogService
from core.services.inline import InlineOutcome, InlineRunner
from core.services.maintenance import sweep_abandoned_uploads
from tests.fakes.catalog import InMemoryCatalog
from tests.fakes.jobs import FakeJobQueue
from tests.fakes.sources import InMemorySources
from tests.fakes.storage import InMemoryBlobStore
from tests.fixtures.documents import minimal_pdf
from tests.port_contracts.job_queue import FUTURE, PAST
from tests.unit.core.services.test_ingest import MD, Env

# ---------------------------------------------------------------- 1. budget


async def test_document_over_the_token_cap_is_rejected_before_any_paid_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ingest_module, "MAX_DOC_TOKENS", 5)
    env = Env()
    with pytest.raises(ValidationFailed, match="too large to index"):
        await env.svc.process_upload(await env.job(MD))
    assert env.embedder.calls == []


async def test_estimated_cost_that_would_cross_the_budget_is_refused() -> None:
    env = Env(spent=Decimal("4.9999999"))  # a tiny document would push it over
    with pytest.raises(BudgetExceeded):
        await env.svc.process_upload(await env.job(MD))
    assert env.embedder.calls == []


async def test_budget_is_rechecked_before_every_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ingest_module, "EMBED_BATCH", 1)
    env = Env()
    big = b"# A\n\n" + b"\n\n".join(b"para %d " % i + b"word " * 600 for i in range(3))
    spends = iter([Decimal(0), Decimal(0), Decimal(5)])  # budget runs out mid-document

    async def month_to_date() -> Decimal:
        return next(spends, Decimal(5))

    env.ledger.month_to_date_usd = month_to_date
    with pytest.raises(BudgetExceeded):
        await env.svc.process_upload(await env.job(big))
    assert len(env.embedder.calls) == 1  # stopped after the first batch


# ---------------------------------------------------------------- 5. runs never left open


async def test_unexpected_crash_still_finishes_the_run() -> None:
    env = Env()

    def explode(data: bytes, mime: str) -> object:
        raise MemoryError("parser blew up")

    env.svc._parser.parse = explode  # pyright: ignore[reportAttributeAccessIssue]
    with pytest.raises(MemoryError):
        await env.svc.process_upload(await env.job(MD))
    assert env.run_status() == "failed"


async def test_malformed_payload_is_a_final_validation_error() -> None:
    env = Env()
    job = await env.job(MD)
    bad = replace(job, payload={**job.payload, "mime": "text/html"})  # kind says md
    with pytest.raises(ValidationFailed, match="Malformed job payload"):
        await env.svc.process_upload(bad)


# ---------------------------------------------------------------- 6, 7. parser bounds


def test_deeply_nested_html_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(formats, "MAX_HTML_DEPTH", 20)
    html = b"<div>" * 50 + b"<p>x</p>" + b"</div>" * 50
    with pytest.raises(ValidationFailed, match="nested too deeply"):
        FormatParser().parse(html, MIME[FileKind.HTML])


def test_html_with_too_many_elements_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(formats, "MAX_HTML_ELEMENTS", 100)
    with pytest.raises(ValidationFailed, match="too large"):
        FormatParser().parse(b"<p>x</p>" * 200, MIME[FileKind.HTML])


def test_pdf_page_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(formats, "MAX_PDF_PAGES", 0)
    with pytest.raises(ValidationFailed, match="more than 0 pages"):
        FormatParser().parse(minimal_pdf(["hi"]), MIME[FileKind.PDF])


def test_text_size_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(formats, "MAX_TEXT_CHARS", 10)
    with pytest.raises(ValidationFailed, match="too much text"):
        FormatParser().parse(b"x" * 50, MIME[FileKind.TEXT])


# ---------------------------------------------------------------- 8. oversized "words"


def test_a_single_huge_word_is_split_to_fit() -> None:
    def chars(text: str) -> int:  # a tokenizer that sees long words as many tokens
        return max(1, len(text) // 4)

    config = ChunkingConfig(target=50, overlap=5)
    chunks = chunk_sections([Section(None, "A" * 5000)], chars, config)
    assert len(chunks) > 1
    assert all(c.token_count <= config.target for c in chunks)


# ---------------------------------------------------------------- 4. inline timeout


async def test_inline_processing_times_out_and_hands_over_to_the_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(inline_module, "INLINE_TIMEOUT_S", 0.05)
    q = FakeJobQueue()
    job_id = await q.enqueue(WorkspaceId(uuid4()), "process_upload", {}, "k", run_after=PAST)

    async def slow(job: Job) -> None:
        await asyncio.sleep(5)

    assert await InlineRunner(q, slow, runner_id="api").run(job_id) is InlineOutcome.DEFERRED
    job = await q.get(job_id)
    assert job.status is JobStatus.FAILED  # retryable: the worker picks it up
    assert job.last_error == "inline processing timed out"


async def test_claim_by_id_respects_run_after() -> None:
    q = FakeJobQueue()
    job_id = await q.enqueue(WorkspaceId(uuid4()), "k", {}, "key", run_after=FUTURE)
    assert await q.claim_by_id(job_id, "api") is None


# ---------------------------------------------------------------- 3. catalog privacy


def _identity(ws: WorkspaceId, role: Role) -> Identity:
    u = UserId(uuid4())
    return Identity(
        user_id=u,
        workspace_id=ws,
        session_id=SessionId(uuid4()),
        email="m@x.dev",
        name="M",
        role=role,
        is_guest=False,
        principals=Principals.for_user(u, ws),
        csrf_secret="c",
    )


def _upload_source(ws: WorkspaceId) -> SourceSummary:
    return SourceSummary(
        id=SourceId(uuid4()),
        name="Uploads",
        connector="upload",
        data_class="private",
        visibility=(f"workspace:{ws}",),
        status="active",
        documents=0,
        last_synced_at=None,
        last_run_status="failed",
        last_run_error="Alice's tax return.pdf is not a PDF",
        pending_jobs=2,
    )


async def test_members_dont_see_other_uploaders_runs_errors_or_pending() -> None:
    ws = WorkspaceId(uuid4())
    catalog = InMemoryCatalog()
    src = _upload_source(ws)
    catalog.source_rows.append((ws, src))
    catalog.run_rows[src.id] = [
        RunSummary(
            id=RunId(uuid4()),
            kind="upload",
            status="failed",
            started_at=None,
            finished_at=None,
            docs_seen=1,
            docs_changed=0,
            docs_failed=1,
            error_summary="x",
            errors=(),
        )
    ]
    service = CatalogService(catalog)
    member = _identity(ws, Role.MEMBER)
    [row] = await service.sources(member)
    assert row.last_run_error is None
    assert row.pending_jobs == 0
    assert await service.runs(member, src.id) == []
    admin = _identity(ws, Role.ADMIN)
    assert (await service.sources(admin))[0].last_run_error is not None
    assert len(await service.runs(admin, src.id)) == 1
    with pytest.raises(NotFound):
        await service.runs(member, SourceId(uuid4()))


# ---------------------------------------------------------------- 9. sweeper race


async def test_sweeper_skips_objects_reuploaded_after_listing() -> None:
    blobs, sources = InMemoryBlobStore(), InMemorySources()
    await blobs.put("uploads/a", b"x", "text/plain")
    await blobs.put("uploads/b", b"x", "text/plain")
    blobs.old_keys = {"uploads/a", "uploads/b"}
    original = blobs.list_older_than

    async def listed_then_reuploaded(prefix: str, age: int, limit: int) -> list[str]:
        keys = await original(prefix, age, limit)
        blobs.old_keys.discard("uploads/b")  # "b" was re-uploaded right after listing
        return keys

    blobs.list_older_than = listed_then_reuploaded  # pyright: ignore[reportAttributeAccessIssue]
    assert await sweep_abandoned_uploads(blobs, sources) == 1
    assert "uploads/a" not in blobs.blobs
    assert "uploads/b" in blobs.blobs


async def test_sweeper_never_deletes_referenced_objects() -> None:
    blobs, sources = InMemoryBlobStore(), InMemorySources()
    await blobs.put("uploads/used", b"x", "text/plain")
    blobs.old_keys = {"uploads/used"}
    sources.used_blobs = {"uploads/used"}
    assert await sweep_abandoned_uploads(blobs, sources) == 0
    assert "uploads/used" in blobs.blobs
