from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from core.domain.documents import (
    Document,
    DocumentUpsert,
    NewChunk,
    ScoredChunk,
    SearchFilters,
    Vector,
)
from core.domain.errors import NotFound
from core.domain.ids import ChunkId, DocumentId, GenerationId, WorkspaceId
from core.domain.principals import Principals
from tests.fakes.system import SequentialIds


@dataclass
class _StoredChunk:
    id: ChunkId
    workspace_id: WorkspaceId
    document_id: DocumentId
    chunk: NewChunk
    acl: tuple[str, ...]
    embeddings: dict[GenerationId, Vector]


class InMemoryIndex:
    """Implements both DocumentStore and SearchIndex over shared in-memory state.

    ACL and workspace filtering happen inside the "query", mirroring the SQL rule.
    """

    def __init__(self, ids: SequentialIds | None = None) -> None:
        self._ids = ids or SequentialIds()
        self._docs: dict[DocumentId, tuple[WorkspaceId, Document]] = {}
        self._by_key: dict[tuple[object, str], DocumentId] = {}
        self._chunks: list[_StoredChunk] = []
        self._next_chunk = 0

    # ---- DocumentStore
    async def upsert_document(self, ws: WorkspaceId, doc: DocumentUpsert) -> DocumentId:
        key = (doc.source_id, doc.external_id)
        existing_id = self._by_key.get(key)
        if existing_id is not None:
            _, old = self._docs[existing_id]
            version = old.version + (1 if old.checksum != doc.checksum else 0)
            updated = replace(
                old,
                title=doc.title,
                uri=doc.uri,
                mime=doc.mime,
                checksum=doc.checksum,
                acl=doc.acl,
                version=version,
                author=doc.author,
                source_updated_at=doc.source_updated_at,
            )
            self._docs[existing_id] = (ws, updated)
            for c in self._chunks:  # visibility change rewrites ACLs without re-embedding
                if c.document_id == existing_id:
                    c.acl = doc.acl
            return existing_id
        doc_id = DocumentId(self._ids.new_uuid())
        self._docs[doc_id] = (
            ws,
            Document(
                id=doc_id,
                source_id=doc.source_id,
                external_id=doc.external_id,
                uri=doc.uri,
                title=doc.title,
                mime=doc.mime,
                checksum=doc.checksum,
                version=1,
                acl=doc.acl,
                author=doc.author,
                source_updated_at=doc.source_updated_at,
            ),
        )
        self._by_key[key] = doc_id
        return doc_id

    async def replace_chunks(
        self,
        ws: WorkspaceId,
        doc_id: DocumentId,
        chunks: Sequence[NewChunk],
        embeddings: Mapping[GenerationId, Sequence[Vector]],
    ) -> None:
        owner = self._docs.get(doc_id)
        if owner is None or owner[0] != ws:
            raise NotFound("document not found")
        for gen, vectors in embeddings.items():
            if len(vectors) != len(chunks):
                raise ValueError(
                    f"generation {gen}: {len(vectors)} vectors for {len(chunks)} chunks"
                )
        self._chunks = [c for c in self._chunks if c.document_id != doc_id]
        for i, chunk in enumerate(chunks):
            self._next_chunk += 1
            self._chunks.append(
                _StoredChunk(
                    id=ChunkId(self._next_chunk),
                    workspace_id=ws,
                    document_id=doc_id,
                    chunk=chunk,
                    acl=owner[1].acl,
                    embeddings={g: v[i] for g, v in embeddings.items()},
                )
            )

    async def get_document(
        self, ws: WorkspaceId, doc_id: DocumentId, principals: Principals
    ) -> Document:
        found = self._docs.get(doc_id)
        if found is None or found[0] != ws or not set(found[1].acl) & set(principals.values):
            raise NotFound("document not found")  # same error for missing and forbidden
        return found[1]

    # ---- SearchIndex
    def _visible(
        self, ws: WorkspaceId, principals: Principals, f: SearchFilters
    ) -> list[_StoredChunk]:
        allowed = set(principals.values)
        out: list[_StoredChunk] = []
        for c in self._chunks:
            if c.workspace_id != ws or not allowed & set(c.acl):
                continue
            doc = self._docs[c.document_id][1]
            if f.source_ids and doc.source_id not in f.source_ids:
                continue
            updated = doc.source_updated_at
            if f.updated_after and (updated is None or updated < f.updated_after):
                continue
            if f.updated_before and (updated is None or updated > f.updated_before):
                continue
            out.append(c)
        return out

    @staticmethod
    def _scored(c: _StoredChunk, score: float) -> ScoredChunk:
        return ScoredChunk(
            chunk_id=c.id,
            document_id=c.document_id,
            ordinal=c.chunk.ordinal,
            content=c.chunk.content,
            score=score,
            heading_path=c.chunk.heading_path,
        )

    async def vector_search(
        self,
        ws: WorkspaceId,
        principals: Principals,
        query_vector: Vector,
        generation: GenerationId,
        k: int,
        filters: SearchFilters,
    ) -> list[ScoredChunk]:
        hits: list[ScoredChunk] = []
        for c in self._visible(ws, principals, filters):
            vec = c.embeddings.get(generation)
            if vec is not None:
                hits.append(self._scored(c, _cosine(query_vector, vec)))
        return sorted(hits, key=lambda h: (-h.score, h.chunk_id))[:k]

    async def keyword_search(
        self, ws: WorkspaceId, principals: Principals, query: str, k: int, filters: SearchFilters
    ) -> list[ScoredChunk]:
        terms = {t for t in query.lower().split() if t}
        hits: list[ScoredChunk] = []
        for c in self._visible(ws, principals, filters):
            words = c.chunk.content.lower().split()
            score = float(sum(words.count(t) for t in terms))
            if score > 0:
                hits.append(self._scored(c, score))
        return sorted(hits, key=lambda h: (-h.score, h.chunk_id))[:k]


def _cosine(a: Vector, b: Vector) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class InMemoryBlobStore:
    def __init__(self) -> None:
        self.blobs: dict[str, tuple[bytes, str]] = {}

    async def put(self, key: str, data: bytes, content_type: str) -> str:
        self.blobs[key] = (data, content_type)
        return key

    async def get(self, ref: str) -> bytes:
        if ref not in self.blobs:
            raise NotFound("blob not found")
        return self.blobs[ref][0]

    async def delete(self, ref: str) -> None:
        self.blobs.pop(ref, None)

    async def presigned_url(self, ref: str, *, expires_in: int = 300) -> str:
        if ref not in self.blobs:
            raise NotFound("blob not found")
        return f"https://blobs.test/{ref}?expires={expires_in}"
