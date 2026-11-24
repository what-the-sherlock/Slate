"""Persistence ports: documents, search, blobs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from core.domain.documents import (
    Document,
    DocumentUpsert,
    NewChunk,
    ScoredChunk,
    SearchFilters,
    Vector,
)
from core.domain.ids import DocumentId, GenerationId, WorkspaceId
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
