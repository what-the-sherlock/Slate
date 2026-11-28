"""Persistence ports: documents, search, blobs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from core.domain.attachments import AttachmentInfo
from core.domain.documents import (
    AttachmentParent,
    ChunkToEmbed,
    Document,
    DocumentPreview,
    DocumentUpsert,
    NewChunk,
    ScoredChunk,
    SearchFilters,
    Vector,
)
from core.domain.ids import ChunkId, DocumentId, GenerationId, SourceId, WorkspaceId
from core.domain.principals import Principals


class DocumentStore(Protocol):
    async def upsert_document(self, ws: WorkspaceId, doc: DocumentUpsert) -> DocumentId:
        """Insert or update by ``(source_id, external_id)``. Idempotent."""
        ...

    async def replace_chunks(
        self,
        ws: WorkspaceId,
        doc_id: DocumentId,
        chunks: Sequence[NewChunk],
        embeddings: Mapping[GenerationId, Sequence[Vector]],
    ) -> None:
        """Atomically replace a document's chunks and their embeddings."""
        ...

    async def get_document(
        self, ws: WorkspaceId, doc_id: DocumentId, principals: Principals
    ) -> Document:
        """Return the document if visible to ``principals``.

        Raises NotFound both when it doesn't exist and when access is denied, so
        callers can't probe for documents they may not see.
        """
        ...

    async def write_document(
        self,
        ws: WorkspaceId,
        doc: DocumentUpsert,
        chunks: Sequence[NewChunk],
        embeddings: Mapping[GenerationId, Sequence[Vector]],
        *,
        data_class: str,
    ) -> tuple[DocumentId, bool]:
        """Upsert the document and replace its chunks and embeddings in ONE transaction.

        Returns (document id, changed). An unchanged checksum is a no-op (changed=False).
        """
        ...

    # Attachments (ADR-0043). Children are identified through their parent, never listed
    # with the source's documents (a sync's prune would otherwise delete them).
    async def child_checksums(self, source_id: SourceId, parent_external_id: str) -> dict[str, str]:
        """external_id -> checksum of this parent's attachment documents."""
        ...

    async def set_attachments(
        self, source_id: SourceId, parent_external_id: str, attachments: Sequence[AttachmentInfo]
    ) -> None:
        """Replace the parent's Tier 0 list (``metadata["attachments"]``); no-op if gone.
        Never changes the checksum, so the parent is not re-embedded."""
        ...

    async def attachment_parent(
        self, ws: WorkspaceId, principals: Principals | None, doc_id: DocumentId
    ) -> AttachmentParent:
        """The parent as the attachment reader needs it, if visible to ``principals``
        (NotFound for missing and forbidden, like ``get_document``). ``None`` only for the
        worker finishing a read that a signed-in request started."""
        ...

    async def chunks_without_embedding(
        self, ws: WorkspaceId, doc_ids: Sequence[DocumentId], generation: GenerationId, limit: int
    ) -> list[ChunkToEmbed]:
        """Chunks of these documents *and their attachments* (in ``ws``) with no vector in
        ``generation``: the lazy Tier 2 of an attachment."""
        ...

    # Backfill (system work across all workspaces; never user-facing, never retrieval).
    async def chunks_missing_embedding(
        self, generation: GenerationId, *, public_only: bool, after: ChunkId | None, limit: int
    ) -> list[ChunkToEmbed]:
        """Live chunks with no vector in ``generation``, by ascending id (keyset paging)."""
        ...

    async def add_embeddings(
        self, generation: GenerationId, items: Sequence[tuple[ChunkId, Vector]]
    ) -> int:
        """Insert vectors; existing (chunk, generation) pairs are left alone. Returns inserted."""
        ...


class SearchIndex(Protocol):
    """Every method filters by workspace and ACL *inside* the query (rule 1)."""

    async def vector_search(
        self,
        ws: WorkspaceId,
        principals: Principals,
        query_vector: Vector,
        generation: GenerationId,
        k: int,
        filters: SearchFilters,
    ) -> list[ScoredChunk]: ...

    async def keyword_search(
        self,
        ws: WorkspaceId,
        principals: Principals,
        query: str,
        k: int,
        filters: SearchFilters,
    ) -> list[ScoredChunk]: ...

    async def recent_chunks(
        self, ws: WorkspaceId, principals: Principals, k: int, filters: SearchFilters
    ) -> list[ScoredChunk]:
        """Chunks of the ``k`` most recently updated visible documents, newest first (the
        last and first chunk of each). Serves "latest" and "what happened" questions,
        which similarity can't rank (ADR-0031)."""
        ...

    async def anchored_chunks(
        self,
        ws: WorkspaceId,
        principals: Principals,
        documents: Sequence[DocumentId],
        query: str,
        k: int,
        filters: SearchFilters,
    ) -> list[ScoredChunk]:
        """Chunks of these documents the caller may still see, best match for ``query``
        first (then document order). The follow-up lane (P1-5): "who owns that?" stays on
        what the last answer cited. Same visibility rule as every lane, in the query."""
        ...

    async def document_preview(
        self, ws: WorkspaceId, principals: Principals, doc_id: DocumentId, max_chunks: int
    ) -> DocumentPreview:
        """Raises NotFound for missing AND forbidden documents (no probing)."""
        ...

    async def neighbours(
        self, ws: WorkspaceId, principals: Principals, around: Sequence[tuple[DocumentId, int]]
    ) -> list[ScoredChunk]:
        """The chunks at ordinal +-1 of each (document, ordinal), under the same visibility
        rule as search (answers widen passages with them; ARCHITECTURE §8 step 6)."""
        ...

    async def attachment_children(
        self, ws: WorkspaceId, principals: Principals, parents: Sequence[DocumentId]
    ) -> list[tuple[DocumentId, DocumentId]]:
        """(child id, parent id) for these documents' indexed attachments the caller may
        see (ADR-0043)."""
        ...

    async def chunks_by_id(
        self, ws: WorkspaceId, principals: Principals, chunk_ids: Sequence[ChunkId]
    ) -> list[ScoredChunk]:
        """These chunks, if the caller may still see them (missing and forbidden ones are
        left out, no probing). Rebuilds a reopened chat's citations (UI_DESIGN §4.3)."""
        ...


@dataclass(frozen=True, slots=True)
class PresignedUpload:
    """POST ``fields`` plus the file (as the last form field) to ``url``."""

    url: str
    fields: dict[str, str]


class BlobStore(Protocol):
    async def put(self, key: str, data: bytes, content_type: str) -> str:
        """Store bytes and return a reference for ``get``/``delete``."""
        ...

    async def get(self, ref: str) -> bytes:
        """Raises NotFound for an unknown reference."""
        ...

    async def delete(self, ref: str) -> None:
        """Idempotent: deleting a missing blob is not an error."""
        ...

    async def presigned_url(self, ref: str, *, expires_in: int = 300) -> str:
        """A short-lived URL that lets a browser download this blob without credentials."""
        ...

    async def presigned_upload(
        self, key: str, *, content_type: str, max_bytes: int, expires_in: int = 600
    ) -> PresignedUpload:
        """A browser form upload straight to storage, enforcing size limits server-side."""
        ...

    async def size_of(self, key: str) -> int | None:
        """Object size in bytes, or None if it doesn't exist (cheap HEAD)."""
        ...

    async def read_head(self, key: str, n: int) -> bytes:
        """The first ``n`` bytes (ranged GET), for sniffing without a full download."""
        ...

    async def list_older_than(self, prefix: str, age_seconds: int, limit: int) -> list[str]:
        """Keys under ``prefix`` last modified more than ``age_seconds`` ago."""
        ...

    async def modified_at(self, key: str) -> datetime | None:
        """Last-modified time (cheap HEAD), or None if the object doesn't exist."""
        ...
