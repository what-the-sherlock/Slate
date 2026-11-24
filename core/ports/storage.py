"""Persistence ports: documents, search, blobs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
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
