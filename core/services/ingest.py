"""process_upload: download -> VERIFY -> parse -> chunk -> embed -> store (ARCHITECTURE §7).

This is the security gate for uploads (ADR-0016 amendment): everything is decided
from ONE capped download. Those exact bytes are hashed, sniffed, fully validated and
parsed, so swapping the object after ``complete`` gains an attacker nothing.

Failure policy: a ValidationFailed (bad file) is final, so the job goes dead and the run
records the reason. Upstream problems (storage, OpenAI) are retryable.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from uuid import UUID

import structlog

from core.domain.chunking import chunk_sections, embedding_text
from core.domain.data_class import classify
from core.domain.documents import DocumentUpsert, NewChunk, Section
from core.domain.errors import AppError, BudgetExceeded, UpstreamUnavailable, ValidationFailed
from core.domain.ids import DocumentId, RunId, SourceId, UserId, WorkspaceId
from core.domain.jobs import Job
from core.domain.uploads import MAX_UPLOAD_BYTES, MIME, SNIFF_BYTES, FileKind, sniff_kind
from core.ports.models import Embedder, Tokenizer
from core.ports.sources import SourceStore
from core.ports.storage import BlobStore, DocumentStore
from core.ports.system import Parser
from core.ports.usage import GenerationStore, UsageEntry, UsageLedger

log = structlog.get_logger("ingest")

EMBED_BATCH = 96
# One document may never cost more than this many tokens (~$0.008 with 3-small), so a
# single hostile or enormous file can't drain the monthly budget (security review).
MAX_DOC_TOKENS = 400_000
# USD per 1M input tokens (ARCHITECTURE §12). Unknown models are costed conservatively.
PRICE_PER_M_TOKENS = {"text-embedding-3-small": Decimal("0.02")}
DEFAULT_PRICE_PER_M = Decimal("0.13")


class Stage:
    """Which pipeline stage is running, so a failure is recorded against the right one."""

    def __init__(self, name: str = "fetch") -> None:
        self.name = name


@dataclass(frozen=True, slots=True)
class IndexResult:
    document_id: DocumentId
    changed: bool
    chunks: int


@dataclass(frozen=True, slots=True)
class UploadPayload:
    blob_key: str
    filename: str
    kind: FileKind
    mime: str
    size: int
    sha256: str
    acl: tuple[str, ...]
    data_class: str
    source_id: SourceId
    external_id: str
    uploader: UserId

    @classmethod
    def from_job(cls, job: Job) -> UploadPayload:
        try:
            return cls._parse(job)
        except (KeyError, ValueError, TypeError) as e:
            raise ValidationFailed("Malformed job payload.") from e

    @classmethod
    def _parse(cls, job: Job) -> UploadPayload:
        p = job.payload
        acl = p.get("acl")
        if not isinstance(acl, list):
            raise ValidationFailed("Malformed job payload (acl).")
        kind = FileKind(str(p["kind"]))
        if str(p["mime"]) != MIME[kind]:
            raise ValidationFailed("Malformed job payload (mime does not match kind).")
        return cls(
            blob_key=str(p["blob_key"]),
            filename=str(p["filename"]),
            kind=FileKind(str(p["kind"])),
            mime=str(p["mime"]),
            size=int(str(p["size"])),
            sha256=str(p["sha256"]),
            acl=tuple(str(a) for a in acl),  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
            data_class=classify(str(p["data_class"])).value,
            source_id=SourceId(UUID(str(p["source_id"]))),
            external_id=str(p["external_id"]),
            uploader=UserId(UUID(str(p["uploader"]))),
        )


class IngestService:
    def __init__(
        self,
        blobs: BlobStore,
        documents: DocumentStore,
        sources: SourceStore,
        parser: Parser,
        tokenizer: Tokenizer,
        embedder: Embedder,
        generations: GenerationStore,
        ledger: UsageLedger,
        *,
        monthly_budget_usd: Decimal,
    ) -> None:
        self._blobs = blobs
        self._documents = documents
        self._sources = sources
        self._parser = parser
        self._tokenizer = tokenizer
        self._embedder = embedder
        self._generations = generations
        self._ledger = ledger
        self._budget = monthly_budget_usd

    async def process_upload(self, job: Job) -> None:
        payload = UploadPayload.from_job(job)
        run = await self._sources.create_run(payload.source_id, "upload")
        bound = log.bind(job_id=str(job.id), run_id=str(run))
        stage = Stage("fetch")
        # The external id includes the SHA-256, so an indexed match means identical bytes:
        # skip the download and, above all, the paid embedding call.
        if await self._sources.find_upload(job.workspace_id, payload.external_id) is not None:
            await self._sources.finish_run(run, status="succeeded", seen=1, changed=0, failed=0)
            bound.info("ingest_skipped_unchanged")
            return
        try:
            data = await self._download(payload)
            stage.name = "parse"
            self._verify(payload, data)
            # Dispatch on the VERIFIED kind (the gate checked these bytes as that kind).
            parsed = await asyncio.to_thread(self._parser.parse, data, MIME[payload.kind])
            result = await self.index_document(
                job.workspace_id,
                DocumentUpsert(
                    source_id=payload.source_id,
                    external_id=payload.external_id,
                    uri=f"upload:{payload.blob_key}",
                    title=parsed.title or payload.filename,
                    mime=payload.mime,
                    checksum=payload.sha256,
                    acl=payload.acl,
                    author=None,
                    metadata={
                        "blob_key": payload.blob_key,
                        "filename": payload.filename,
                        "size": payload.size,
                        "uploader": str(payload.uploader),
                    },
                ),
                parsed.sections,
                data_class=payload.data_class,
                stage=stage,
            )
            changed = result.changed
        except AppError as e:
            await self._fail_run(run, payload, stage.name, e)
            bound.warning("ingest_failed", stage=stage.name, error_code=e.code)
            raise
        except BaseException as e:  # crash/cancel: never leave the run "running" forever
            await asyncio.shield(
                self._fail_run(
                    run,
                    payload,
                    stage.name,
                    UpstreamUnavailable(f"Processing stopped ({type(e).__name__})."),
                )
            )
            bound.error("ingest_crashed", stage=stage.name, error_type=type(e).__name__)
            raise
        await self._sources.finish_run(
            run, status="succeeded", seen=1, changed=int(changed), failed=0
        )
        bound.info("ingest_succeeded", chunks=result.chunks, changed=changed)

    async def index_document(
        self,
        ws: WorkspaceId,
        doc: DocumentUpsert,
        sections: Sequence[Section],
        *,
        data_class: str,
        stage: Stage | None = None,
    ) -> IndexResult:
        """chunk -> embed -> store for one already-parsed document (any source).

        Budget-guarded and recorded in the usage ledger. Idempotent: an unchanged
        checksum writes nothing (``changed=False``).
        """
        stage = stage or Stage()
        stage.name = "chunk"
        chunks = chunk_sections(sections, self._tokenizer.count)
        if not chunks:
            raise ValidationFailed("No text found in this file.")
        total_tokens = sum(c.token_count for c in chunks)
        if total_tokens > MAX_DOC_TOKENS:
            raise ValidationFailed(
                "This document is too large to index (over 400,000 tokens of text)."
            )
        stage.name = "embed"
        await self._check_budget(self._estimate_usd(total_tokens))
        generation = await self._generations.ensure_present(
            self._embedder.model, self._embedder.dims
        )
        vectors = await self._embed(ws, data_class, doc.title, chunks)
        stage.name = "store"
        doc = replace(doc, metadata={**doc.metadata, "chunks": len(chunks)})
        doc_id, changed = await self._documents.write_document(
            ws, doc, chunks, {generation: vectors}, data_class=data_class
        )
        return IndexResult(doc_id, changed, len(chunks))

    async def _download(self, payload: UploadPayload) -> bytes:
        size = await self._blobs.size_of(payload.blob_key)
        if size is None:
            raise ValidationFailed("The uploaded file is missing from storage.")
        if size > MAX_UPLOAD_BYTES:  # byte cap before downloading anything
            raise ValidationFailed("The stored file is larger than 25 MB.")
        data = await self._blobs.get(payload.blob_key)
        if len(data) > MAX_UPLOAD_BYTES:
            raise ValidationFailed("The stored file is larger than 25 MB.")
        return data

    @staticmethod
    def _verify(payload: UploadPayload, data: bytes) -> None:
        """The upload security gate: these exact bytes must be what was announced."""
        if hashlib.sha256(data).hexdigest() != payload.sha256:
            raise ValidationFailed("The stored file doesn't match the file that was uploaded.")
        if sniff_kind(data[:SNIFF_BYTES], payload.kind) is None:
            raise ValidationFailed(f"This doesn't look like a valid .{payload.kind.value} file.")

    def _estimate_usd(self, tokens: int) -> Decimal:
        price = PRICE_PER_M_TOKENS.get(self._embedder.model, DEFAULT_PRICE_PER_M)
        return price * tokens / Decimal(1_000_000)

    async def _check_budget(self, upcoming_usd: Decimal = Decimal(0)) -> None:
        """Refuse if this spend would cross the budget. Called before the first paid
        call (with the whole document's estimate) and again before every batch.
        """
        spent = await self._ledger.month_to_date_usd()
        if spent + upcoming_usd > self._budget:
            raise BudgetExceeded(
                "This month's AI budget is used up; processing resumes next month."
            )
        if spent >= self._budget * Decimal("0.8"):
            log.warning("budget_80_percent", spent_usd=str(spent), budget_usd=str(self._budget))

    async def _embed(
        self, ws: WorkspaceId, data_class: str, title: str, chunks: Sequence[NewChunk]
    ) -> list[Sequence[float]]:
        texts = [embedding_text(title, c) for c in chunks]
        vectors: list[Sequence[float]] = []
        for start in range(0, len(texts), EMBED_BATCH):
            batch = texts[start : start + EMBED_BATCH]
            await self._check_budget(
                self._estimate_usd(sum(c.token_count for c in chunks[start : start + EMBED_BATCH]))
            )
            result = await self._embedder.embed_documents(batch)
            vectors.extend(result.vectors)
            price = PRICE_PER_M_TOKENS.get(self._embedder.model, DEFAULT_PRICE_PER_M)
            await self._ledger.record(
                UsageEntry(
                    workspace_id=ws,
                    provider="openai",
                    model=self._embedder.model,
                    kind="embed",
                    data_class=data_class,
                    tokens_in=result.tokens,
                    tokens_out=0,
                    cost_usd=price * result.tokens / Decimal(1_000_000),
                )
            )
        return vectors

    async def _fail_run(self, run: RunId, payload: UploadPayload, stage: str, e: AppError) -> None:
        await self._sources.record_error(
            run,
            external_id=payload.external_id,
            stage=stage,
            message=e.detail[:1000],
            retryable=e.retryable,
        )
        await self._sources.finish_run(
            run, status="failed", seen=1, changed=0, failed=1, error_summary=e.detail[:300]
        )
