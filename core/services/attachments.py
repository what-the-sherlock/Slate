"""Attachments: bytes -> state -> child document (ADR-0043).

Shared by every connector that carries files (Gmail, IMAP/.mbox, Discord), at sync and on
demand. Connectors only fetch (rule 2); this engine decides what a file is from its
*bytes* (never its name or declared type), parses it with the parsers uploads use, and
stores the text as a child document: keyword-searchable at no cost (Tier 1). Vectors come
later, only for attachments a question reaches (``IngestService.embed_documents``).

The database keeps a child no more visible than its parent (migration 0017): its ACL,
deletion and data class are copied from the parent, whatever this code passes.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from uuid import UUID

import structlog

from core.domain.attachments import (
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENT_TOKENS,
    MAX_ATTACHMENTS_READ,
    PARSE_TIMEOUT_S,
    RETRYABLE,
    AttachmentBlob,
    AttachmentInfo,
    AttachmentState,
    child_external_id,
    claimed_kind,
    first_pass_state,
    looks_encrypted,
    mime_for,
)
from core.domain.documents import AttachmentParent, DocumentUpsert, Section
from core.domain.errors import (
    AppError,
    BudgetExceeded,
    NotFound,
    PermissionDenied,
    RateLimited,
    ReconnectRequired,
    UnreadableFile,
    UpstreamUnavailable,
    ValidationFailed,
)
from core.domain.ids import DocumentId, SourceId, WorkspaceId
from core.domain.jobs import Job
from core.domain.principals import Principals
from core.domain.uploads import SNIFF_BYTES, FileKind, sniff_kind
from core.ports.attachments import AttachmentFetcher
from core.ports.jobs import JobQueue
from core.ports.sources import SourceStore
from core.ports.storage import DocumentStore
from core.ports.system import Parser
from core.services.ingest import IngestService

log = structlog.get_logger()

MAX_ON_DEMAND_FILES = 3  # per document per request: answers stay fast

_REASON_STATE = {
    "encrypted": AttachmentState.ENCRYPTED,
    "empty": AttachmentState.EMPTY,
    "too_large": AttachmentState.TOO_LARGE,
}


@dataclass(frozen=True, slots=True)
class Prepared:
    """An attachment after its bytes were checked: a final state, and text when readable."""

    info: AttachmentInfo
    sections: tuple[Section, ...] = field(default=(), repr=False)
    mime: str = "text/plain"
    checksum: str = ""


@dataclass(frozen=True, slots=True)
class ParentRef:
    """The document an attachment came with (already written: the FK needs it)."""

    workspace_id: WorkspaceId
    source_id: SourceId
    external_id: str
    title: str
    uri: str
    acl: tuple[str, ...]
    data_class: str
    author: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class AttachmentIndexer:
    def __init__(
        self,
        ingest: IngestService,
        documents: DocumentStore,
        sources: SourceStore,
        parser: Parser | None,
    ) -> None:
        self._ingest = ingest
        self._documents = documents
        self._sources = sources
        self._parser = parser

    # ------------------------------------------------------------ read the bytes

    async def prepare(
        self, infos: Sequence[AttachmentInfo], blobs: Sequence[AttachmentBlob]
    ) -> list[Prepared]:
        """Every listed attachment with a final state; those whose bytes the connector
        kept are sniffed and parsed. Never raises for a bad file (it gets a state)."""
        by_key = {b.info.key: b for b in blobs}
        out: list[Prepared] = []
        for position, info in enumerate(infos):
            decided = first_pass_state(info, position=position)
            if decided is not None:
                out.append(Prepared(info.with_state(decided)))
                continue
            blob = by_key.get(info.key)
            if blob is None:  # not fetched (on demand, or over the page's memory cap)
                out.append(Prepared(info.with_state(AttachmentState.LISTED)))
                continue
            out.append(await self.read(blob))
        return out

    async def read(self, blob: AttachmentBlob) -> Prepared:
        """One file's bytes -> its state and text (the upload gate, ADR-0016/0043)."""
        info, data = blob.info, blob.data
        info = replace(info, size=len(data) or info.size)
        if len(data) > MAX_ATTACHMENT_BYTES:
            return Prepared(info.with_state(AttachmentState.TOO_LARGE))
        if not data:
            return Prepared(info.with_state(AttachmentState.EMPTY))
        if info.mime == "message/rfc822":  # the mail adapter already turned it into text
            kind = FileKind.TEXT
        else:
            claimed = claimed_kind(info.name, info.mime)
            if claimed is None:
                return Prepared(info.with_state(AttachmentState.UNSUPPORTED))
            kind = claimed
        head = data[:SNIFF_BYTES]
        if looks_encrypted(head, kind):
            return Prepared(info.with_state(AttachmentState.ENCRYPTED))
        if sniff_kind(head, kind) is None:
            return Prepared(info.with_state(AttachmentState.UNREADABLE))
        mime = mime_for(kind)
        if self._parser is None or not self._parser.supports(mime):
            return Prepared(info.with_state(AttachmentState.UNSUPPORTED))
        try:
            # Off the event loop (inline syncs and answers run in a request). A timeout
            # abandons the result; the parser's own caps bound the thread's work.
            parsed = await asyncio.wait_for(
                asyncio.to_thread(self._parser.parse, data, mime), PARSE_TIMEOUT_S
            )
        except TimeoutError:
            log.info("attachment.parse_timeout", kind=kind.value)
            return Prepared(info.with_state(AttachmentState.UNREADABLE))
        except UnreadableFile as e:
            state = _REASON_STATE.get(e.reason, AttachmentState.UNREADABLE)
            return Prepared(info.with_state(state))
        except ValidationFailed:
            return Prepared(info.with_state(AttachmentState.UNREADABLE))
        label = info.name
        sections = tuple(
            Section(f"{label} > {s.heading_path}" if s.heading_path else label, s.text)
            for s in parsed.sections
            if s.text.strip()
        )
        if not sections:
            return Prepared(info.with_state(AttachmentState.EMPTY))
        return Prepared(
            info.with_state(AttachmentState.TEXT),
            sections,
            mime,
            hashlib.sha256(data).hexdigest(),
        )

    # ------------------------------------------------------------ store the text

    async def write(
        self, parent: ParentRef, prepared: Sequence[Prepared], *, prune: bool = True
    ) -> list[AttachmentInfo]:
        """Write each readable attachment as the parent's child (keyword-only), and
        delete children whose file is no longer in the conversation (``prune``). Returns
        the final Tier 0 list; when it differs from ``prepared``, the parent's metadata
        is corrected, so a failed write becomes ``listed`` (re-read on demand), never a
        ``text`` with nothing behind it."""
        existing = await self._documents.child_checksums(parent.source_id, parent.external_id)
        keep: set[str] = set()
        final: list[AttachmentInfo] = []
        for p in prepared:
            if p.info.state is not AttachmentState.TEXT:
                final.append(p.info)
                continue
            ext = child_external_id(parent.external_id, p.info.key)
            keep.add(ext)
            if not p.sections:  # read earlier: an on-demand pass carries it unchanged
                state = AttachmentState.TEXT if ext in existing else AttachmentState.LISTED
                final.append(p.info.with_state(state, truncated=p.info.truncated))
                continue
            final.append(await self._write_one(parent, p, ext))
        if prune:
            stale = [e for e in existing if e not in keep]
            if stale:
                await self._sources.delete_documents(parent.source_id, stale)
        if [f.to_meta() for f in final] != [p.info.to_meta() for p in prepared]:
            await self._documents.set_attachments(parent.source_id, parent.external_id, final)
        return final

    async def prune(
        self, source_id: SourceId, parent_external_id: str, keep: Sequence[AttachmentInfo]
    ) -> int:
        """Delete this parent's children whose file is no longer in it (a Discord message
        edited to drop a file): its text must not stay searchable. Returns how many."""
        existing = await self._documents.child_checksums(source_id, parent_external_id)
        wanted = {child_external_id(parent_external_id, f.key) for f in keep}
        stale = [e for e in existing if e not in wanted]
        if not stale:
            return 0
        return await self._sources.delete_documents(source_id, stale)

    async def _write_one(self, parent: ParentRef, p: Prepared, ext: str) -> AttachmentInfo:
        title = f"{p.info.name} (attached to: {parent.title})"[:500]
        try:
            result = await self._ingest.index_document(
                parent.workspace_id,
                DocumentUpsert(
                    source_id=parent.source_id,
                    external_id=ext,
                    uri=parent.uri,  # opening an attachment opens its conversation
                    title=title,
                    mime=p.mime,
                    checksum=p.checksum,
                    acl=parent.acl,  # the database overwrites it with the parent's anyway
                    author=parent.author,
                    source_created_at=parent.created_at,
                    source_updated_at=parent.updated_at,
                    metadata={
                        "kind": "attachment",
                        "attachment_key": p.info.key,
                        "file_name": p.info.name,
                        "parent_title": parent.title[:300],
                    },
                    parent_external_id=parent.external_id,
                ),
                p.sections,
                data_class=parent.data_class,
                embed=False,
                max_tokens=MAX_ATTACHMENT_TOKENS,
            )
        except BudgetExceeded:  # can't happen without embedding; never swallow it
            raise
        except ValidationFailed:
            return p.info.with_state(AttachmentState.EMPTY)
        except AppError as e:
            log.warning("attachment.write_failed", code=e.code, retryable=e.retryable)
            return p.info.with_state(AttachmentState.LISTED)
        except Exception as e:
            # Two reads of the same file at once collide on (source, external id): when
            # the other one stored these exact bytes, the file is indexed. Otherwise it was
            # a database hiccup: re-read on demand later rather than claim text that
            # isn't there.
            stored = await self._documents.child_checksums(parent.source_id, parent.external_id)
            if stored.get(ext) == p.checksum:
                return p.info.with_state(AttachmentState.TEXT)
            log.warning("attachment.write_failed", error=type(e).__name__)
            return p.info.with_state(AttachmentState.LISTED)
        return p.info.with_state(AttachmentState.TEXT, truncated=result.truncated)


@dataclass(frozen=True, slots=True)
class ReadOutcome:
    attachments: tuple[AttachmentInfo, ...]
    pending: bool  # some files still need reading (deadline, file limit, temporary error)


class AttachmentReader:
    """On-demand reads (ADR-0043): a question reached a document whose files weren't read
    at sync (Discord; Gmail parts too big to come inline; files over a run's memory cap).
    Bounded by a deadline and a per-call file count; what doesn't fit stays ``listed``
    for the worker to finish."""

    def __init__(
        self,
        documents: DocumentStore,
        indexer: AttachmentIndexer,
        fetchers: Mapping[str, AttachmentFetcher],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._documents = documents
        self._indexer = indexer
        self._fetchers = dict(fetchers)
        self._clock = clock

    def can_read(self, connector: str | None) -> bool:
        return connector is not None and connector in self._fetchers

    async def read(
        self,
        ws: WorkspaceId,
        principals: Principals | None,
        parent_id: DocumentId,
        *,
        deadline: float | None,
        max_files: int = MAX_ON_DEMAND_FILES,
    ) -> ReadOutcome:
        """Read this document's unread files. ``principals`` is the asking user (ACL
        re-checked here); None only in the worker, finishing a read a request started."""
        try:
            parent = await self._documents.attachment_parent(ws, principals, parent_id)
        except NotFound:
            return ReadOutcome((), pending=False)
        fetcher = self._fetchers.get(parent.connector)
        infos = list(parent.attachments)
        if not infos and fetcher is not None:
            try:
                infos = await fetcher.discover(parent)
            except (RateLimited, UpstreamUnavailable, ReconnectRequired, PermissionDenied):
                return ReadOutcome((), pending=True)
        if not infos:
            return ReadOutcome((), pending=False)
        prepared: list[Prepared] = []
        pending = False
        tried = 0
        # A message's files can't be changed in place (only removed), so a child already
        # stored for this key is this file read: no second download after a re-sync reset
        # the parent's list to "listed".
        stored = await self._documents.child_checksums(parent.source_id, parent.external_id)
        for position, info in enumerate(infos):
            if child_external_id(parent.external_id, info.key) in stored:
                prepared.append(Prepared(info.with_state(AttachmentState.TEXT)))
                continue
            if info.state not in RETRYABLE or fetcher is None:
                prepared.append(Prepared(info))
                continue
            decided = first_pass_state(info, position=position)
            if decided is not None:
                prepared.append(Prepared(info.with_state(decided)))
                continue
            if tried >= max_files or (deadline is not None and self._clock() > deadline):
                pending = True
                prepared.append(Prepared(info.with_state(AttachmentState.LISTED)))
                continue
            tried += 1
            got = await self._fetch_one(fetcher, parent, info)
            pending = pending or got.info.state is AttachmentState.LISTED
            prepared.append(got)
        final = await self._indexer.write(
            ParentRef(
                workspace_id=ws, source_id=parent.source_id, external_id=parent.external_id,
                title=parent.title, uri=parent.uri, acl=parent.acl,
                data_class=parent.data_class, author=parent.author,
                created_at=parent.updated_at, updated_at=parent.updated_at,
            ),
            prepared,
            prune=False,  # only a sync knows which files left the conversation
        )  # fmt: skip
        # The parent's stored list must match what was just learned (``write`` only
        # compares with what it was handed, which a read already moved past ``listed``).
        if [f.to_meta() for f in final] != [a.to_meta() for a in parent.attachments]:
            await self._documents.set_attachments(parent.source_id, parent.external_id, final)
        return ReadOutcome(tuple(final), pending=pending)

    async def _fetch_one(
        self, fetcher: AttachmentFetcher, parent: AttachmentParent, info: AttachmentInfo
    ) -> Prepared:
        try:
            data = await fetcher.fetch(parent, info)
        except UnreadableFile as e:
            state = _REASON_STATE.get(e.reason, AttachmentState.UNREADABLE)
            return Prepared(info.with_state(state))
        except (RateLimited, UpstreamUnavailable):
            return Prepared(info.with_state(AttachmentState.LISTED))  # try again later
        except (ReconnectRequired, PermissionDenied):
            log.info("attachment.fetch_denied", connector=parent.connector)
            return Prepared(info.with_state(AttachmentState.LISTED))
        if data is None:
            return Prepared(info.with_state(AttachmentState.GONE))
        return await self._indexer.read(AttachmentBlob(info, data))


READ_ATTACHMENTS = "read_attachments"
MAX_JOB_PARENTS = 20


class AttachmentJobs:
    """The ``read_attachments`` job (worker): finish reads a request ran out of time for,
    then add vectors (Tier 2) to the attachments those documents hold, so the next
    question also finds them by meaning. Idempotent: reading skips files already read,
    embedding skips chunks that have a vector."""

    def __init__(self, reader: AttachmentReader, ingest: IngestService, jobs: JobQueue) -> None:
        self._reader = reader
        self._ingest = ingest
        self._jobs = jobs

    async def enqueue(self, ws: WorkspaceId, parents: Sequence[DocumentId], *, hour: int) -> bool:
        """Queue one job for these documents (deduplicated per set and hour)."""
        ids = sorted({str(p) for p in parents})[:MAX_JOB_PARENTS]
        if not ids:
            return False
        digest = hashlib.sha256(",".join(ids).encode()).hexdigest()[:24]
        await self._jobs.enqueue(
            ws, READ_ATTACHMENTS, {"parents": ids}, f"{READ_ATTACHMENTS}:{digest}:{hour}"
        )
        return True

    async def handle(self, job: Job) -> None:
        raw = job.payload.get("parents")
        parents: list[DocumentId] = []
        for value in raw if isinstance(raw, list) else []:  # pyright: ignore[reportUnknownVariableType]
            try:
                parents.append(DocumentId(UUID(str(value))))  # pyright: ignore[reportUnknownArgumentType]
            except ValueError:
                continue
        for parent in parents[:MAX_JOB_PARENTS]:
            await self._reader.read(
                job.workspace_id, None, parent, deadline=None, max_files=MAX_ATTACHMENTS_READ
            )
        try:
            # Email and Discord are always private data (fail closed in the ledger).
            embedded = await self._ingest.embed_documents(
                job.workspace_id, parents, data_class="private"
            )
        except BudgetExceeded:
            # Keyword search already finds them; vectors wait for next month's budget.
            # Retrying now would only fail again, so the job ends here.
            log.info("attachments.embed_skipped", reason="budget")
            embedded = 0
        log.info("attachments.job_done", parents=len(parents), embedded=embedded)
