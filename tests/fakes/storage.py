from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from core.domain.documents import (
    ChunkToEmbed,
    Document,
    DocumentPreview,
    DocumentUpsert,
    NewChunk,
    PreviewChunk,
    ScoredChunk,
    SearchFilters,
    Vector,
)
from core.domain.errors import NotFound
from core.domain.ids import ChunkId, DocumentId, GenerationId, WorkspaceId
from core.domain.principals import Principals
from core.ports.storage import PresignedUpload
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
        self.data_classes: dict[DocumentId, str] = {}
        # source id -> connector name, for the connector filter and prompt metadata
        # (sources not listed are uploads)
        self.connectors: dict[object, str] = {}
        self._next_chunk = 0

    # ---- helpers for InMemorySources (GitHub sync: checksums and pruning)
    def checksums(self, source_id: object, prefix: str) -> dict[str, str]:
        return {
            ext: self._docs[doc_id][1].checksum
            for (src, ext), doc_id in self._by_key.items()
            if src == source_id and ext.startswith(prefix) and doc_id in self._docs
        }

    def versions(self, source_id: object, prefix: str) -> dict[str, tuple[str, datetime | None]]:
        return {
            ext: (self._docs[doc_id][1].checksum, self._docs[doc_id][1].source_updated_at)
            for (src, ext), doc_id in self._by_key.items()
            if src == source_id and ext.startswith(prefix) and doc_id in self._docs
        }

    def delete(self, source_id: object, external_ids: Sequence[str]) -> int:
        n = 0
        for ext in external_ids:
            doc_id = self._by_key.pop((source_id, ext), None)
            if doc_id is None:
                continue
            self._docs.pop(doc_id, None)
            self._chunks = [c for c in self._chunks if c.document_id != doc_id]
            n += 1
        return n

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

    async def write_document(
        self,
        ws: WorkspaceId,
        doc: DocumentUpsert,
        chunks: Sequence[NewChunk],
        embeddings: Mapping[GenerationId, Sequence[Vector]],
        *,
        data_class: str,
    ) -> tuple[DocumentId, bool]:
        key = (doc.source_id, doc.external_id)
        existing = self._by_key.get(key)
        if existing is not None and self._docs[existing][1].checksum == doc.checksum:
            return existing, False
        doc_id = await self.upsert_document(ws, doc)
        await self.replace_chunks(ws, doc_id, chunks, embeddings)
        self.data_classes[doc_id] = data_class
        return doc_id, True

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
            if f.connectors and self._connector(doc) not in f.connectors:
                continue
            updated = doc.source_updated_at
            if f.updated_after and (updated is None or updated < f.updated_after):
                continue
            if f.author and f.author.lower() not in (doc.author or "").lower():
                continue
            if f.updated_before and (updated is None or updated > f.updated_before):
                continue
            out.append(c)
        return out

    def _connector(self, doc: Document) -> str:
        return self.connectors.get(doc.source_id, "upload")

    def _scored(self, c: _StoredChunk, score: float) -> ScoredChunk:
        doc = self._docs[c.document_id][1]
        return ScoredChunk(
            chunk_id=c.id,
            document_id=c.document_id,
            ordinal=c.chunk.ordinal,
            content=c.chunk.content,
            score=score,
            heading_path=c.chunk.heading_path,
            title=doc.title,
            data_class=self.data_classes.get(c.document_id, "private"),
            updated_at=doc.source_updated_at,
            author=doc.author,
            uri=doc.uri,
            connector=self._connector(doc),
        )

    async def recent_chunks(
        self, ws: WorkspaceId, principals: Principals, k: int, filters: SearchFilters
    ) -> list[ScoredChunk]:
        visible = self._visible(ws, principals, filters)
        epoch = datetime.min.replace(tzinfo=UTC)
        by_doc: dict[DocumentId, list[_StoredChunk]] = {}
        for c in visible:
            by_doc.setdefault(c.document_id, []).append(c)
        newest = sorted(
            by_doc,
            key=lambda d: (self._docs[d][1].source_updated_at or epoch, str(d)),
            reverse=True,
        )[:k]
        out: list[_StoredChunk] = []
        for d in newest:
            ordered = sorted(by_doc[d], key=lambda c: c.chunk.ordinal)
            out += [ordered[-1]] + ([ordered[0]] if len(ordered) > 1 else [])
        return [self._scored(c, float(len(out) - i)) for i, c in enumerate(out)]

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

    async def neighbours(
        self, ws: WorkspaceId, principals: Principals, around: Sequence[tuple[DocumentId, int]]
    ) -> list[ScoredChunk]:
        wanted = {(d, o + step) for d, o in around for step in (-1, 1)}
        return [
            self._scored(c, 0.0)
            for c in self._visible(ws, principals, SearchFilters())
            if (c.document_id, c.chunk.ordinal) in wanted
        ]

    async def document_preview(
        self, ws: WorkspaceId, principals: Principals, doc_id: DocumentId, max_chunks: int
    ) -> DocumentPreview:
        doc = await self.get_document(ws, doc_id, principals)  # NotFound if not visible
        chunks = sorted(
            (c for c in self._chunks if c.document_id == doc_id), key=lambda c: c.chunk.ordinal
        )[:max_chunks]
        return DocumentPreview(
            id=doc.id,
            source_id=doc.source_id,
            source_name="Uploads",
            title=doc.title,
            uri=doc.uri,
            mime=doc.mime,
            source_updated_at=doc.source_updated_at,
            chunks=tuple(
                PreviewChunk(c.chunk.ordinal, c.chunk.content, c.chunk.heading_path) for c in chunks
            ),
        )

    # ---- backfill
    async def chunks_missing_embedding(
        self, generation: GenerationId, *, public_only: bool, after: ChunkId | None, limit: int
    ) -> list[ChunkToEmbed]:
        out: list[ChunkToEmbed] = []
        for c in sorted(self._chunks, key=lambda c: c.id):
            if after is not None and c.id <= after:
                continue
            if generation in c.embeddings:
                continue
            if public_only and self.data_classes.get(c.document_id) != "public":
                continue
            doc = self._docs[c.document_id][1]
            out.append(
                ChunkToEmbed(
                    c.id,
                    c.workspace_id,
                    doc.title,
                    c.chunk.heading_path,
                    c.chunk.content,
                    c.chunk.ordinal,
                )
            )
            if len(out) == limit:
                break
        return out

    async def add_embeddings(
        self, generation: GenerationId, items: Sequence[tuple[ChunkId, Vector]]
    ) -> int:
        by_id = {c.id: c for c in self._chunks}
        added = 0
        for chunk_id, vector in items:
            c = by_id.get(chunk_id)
            if c is not None and generation not in c.embeddings:
                c.embeddings[generation] = vector
                added += 1
        return added


def _cosine(a: Vector, b: Vector) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class InMemoryBlobStore:
    def __init__(self) -> None:
        self.blobs: dict[str, tuple[bytes, str]] = {}
        self.upload_policies: dict[str, tuple[str, int]] = {}
        self.old_keys: set[str] = set()  # keys the sweeper should consider stale

    async def put(self, key: str, data: bytes, content_type: str) -> str:
        self.blobs[key] = (data, content_type)
        return key

    async def get(self, ref: str) -> bytes:
        if ref not in self.blobs:
            raise NotFound("blob not found")
        return self.blobs[ref][0]

    async def delete(self, ref: str) -> None:
        self.blobs.pop(ref, None)

    async def presigned_upload(
        self, key: str, *, content_type: str, max_bytes: int, expires_in: int = 600
    ) -> PresignedUpload:
        self.upload_policies[key] = (content_type, max_bytes)
        return PresignedUpload(url="https://blobs.test/upload", fields={"key": key})

    async def size_of(self, key: str) -> int | None:
        return len(self.blobs[key][0]) if key in self.blobs else None

    async def read_head(self, key: str, n: int) -> bytes:
        if key not in self.blobs:
            raise NotFound("blob not found")
        return self.blobs[key][0][:n]

    async def list_older_than(self, prefix: str, age_seconds: int, limit: int) -> list[str]:
        return [k for k in self.blobs if k.startswith(prefix) and k in self.old_keys][:limit]

    async def modified_at(self, key: str) -> datetime | None:
        if key not in self.blobs:
            return None
        return datetime(2000, 1, 1, tzinfo=UTC) if key in self.old_keys else datetime.now(UTC)

    async def presigned_url(self, ref: str, *, expires_in: int = 300) -> str:
        if ref not in self.blobs:
            raise NotFound("blob not found")
        return f"https://blobs.test/{ref}?expires={expires_in}"
