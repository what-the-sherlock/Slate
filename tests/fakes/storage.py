from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from core.domain.attachments import AttachmentInfo, from_meta
from core.domain.chunking import search_context
from core.domain.documents import (
    AttachmentParent,
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
from core.domain.ids import ChunkId, DocumentId, GenerationId, SourceId, WorkspaceId
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
        # What Postgres stores in chunks.search_context (P1-3), per document.
        self.search_contexts: dict[DocumentId, str] = {}
        # source id -> connector name, for the connector filter and prompt metadata
        # (sources not listed are uploads)
        self.connectors: dict[object, str] = {}
        self._next_chunk = 0
        # ADR-0043, mirroring migration 0017: child -> parent, and each document's metadata.
        self.parents: dict[DocumentId, DocumentId] = {}
        self.metadata: dict[DocumentId, dict[str, object]] = {}

    # ---- helpers for InMemorySources (GitHub sync: checksums and pruning)
    def checksums(self, source_id: object, prefix: str) -> dict[str, str]:
        return {  # attachments are listed through their parent, never here (ADR-0043)
            ext: self._docs[doc_id][1].checksum
            for (src, ext), doc_id in self._by_key.items()
            if src == source_id and ext.startswith(prefix) and doc_id in self._docs
            and doc_id not in self.parents
        }  # fmt: skip

    def versions(self, source_id: object, prefix: str) -> dict[str, tuple[str, datetime | None]]:
        return {
            ext: (self._docs[doc_id][1].checksum, self._docs[doc_id][1].source_updated_at)
            for (src, ext), doc_id in self._by_key.items()
            if src == source_id and ext.startswith(prefix) and doc_id in self._docs
            and doc_id not in self.parents
        }  # fmt: skip

    def delete(self, source_id: object, external_ids: Sequence[str]) -> int:
        n = 0
        for ext in external_ids:
            doc_id = self._by_key.pop((source_id, ext), None)
            if doc_id is None:
                continue
            self._drop(doc_id)
            n += 1
        return n

    def _drop(self, doc_id: DocumentId) -> None:
        """Delete a document; its attachments cascade (the FK in migration 0017)."""
        for child in [c for c, p in self.parents.items() if p == doc_id]:
            key = next((k for k, d in self._by_key.items() if d == child), None)
            if key is not None:
                del self._by_key[key]
            self._drop(child)
        self.parents.pop(doc_id, None)
        self.metadata.pop(doc_id, None)
        self._docs.pop(doc_id, None)
        self._chunks = [c for c in self._chunks if c.document_id != doc_id]

    def _parent_of(self, doc: DocumentUpsert) -> DocumentId | None:
        if doc.parent_external_id is None:
            return None
        parent = self._by_key.get((doc.source_id, doc.parent_external_id))
        if parent is None or parent in self.parents:
            raise NotFound("attachment parent not found")
        return parent

    # ---- DocumentStore
    async def upsert_document(self, ws: WorkspaceId, doc: DocumentUpsert) -> DocumentId:
        parent = self._parent_of(doc)
        if parent is not None:  # the trigger: a child always carries its parent's ACL
            doc = replace(doc, acl=self._docs[parent][1].acl)
        doc_id = await self._upsert_plain(ws, doc)
        self.metadata[doc_id] = dict(doc.metadata)
        if parent is not None:
            self.parents[doc_id] = parent
        for child, p in self.parents.items():  # and follows it when it changes
            if p == doc_id:
                self._docs[child] = (ws, replace(self._docs[child][1], acl=doc.acl))
                for c in self._chunks:
                    if c.document_id == child:
                        c.acl = doc.acl
        return doc_id

    async def _upsert_plain(self, ws: WorkspaceId, doc: DocumentUpsert) -> DocumentId:
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
            self.metadata[existing] = dict(doc.metadata)  # metadata is always refreshed
            return existing, False
        doc_id = await self.upsert_document(ws, doc)
        self.search_contexts[doc_id] = search_context(doc.title, doc.author, doc.metadata)
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
            # Conversation filters read the parent's metadata for an attachment (ADR-0043).
            meta = self.metadata.get(self.parents.get(c.document_id, c.document_id), {})
            if f.sent is not None:
                folders = meta.get("folders")
                wanted = "sent" if f.sent else "inbox"
                if not isinstance(folders, list) or wanted not in folders:
                    continue
            if f.person:
                people = " ".join(str(p) for p in meta.get("people") or [])  # pyright: ignore[reportGeneralTypeIssues]
                if f.person.lower() not in f"{doc.author or ''} {people}".lower():
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
            parent_id=self.parents.get(c.document_id),
            attachments=tuple(from_meta(self.metadata.get(c.document_id, {}).get("attachments"))),
        )

    async def anchored_chunks(
        self,
        ws: WorkspaceId,
        principals: Principals,
        documents: Sequence[DocumentId],
        query: str,
        k: int,
        filters: SearchFilters,
    ) -> list[ScoredChunk]:
        wanted = set(documents)
        terms = {t for t in query.lower().split() if t}
        order = {d: i for i, d in enumerate(documents)}
        mine = [c for c in self._visible(ws, principals, filters) if c.document_id in wanted]
        ranked = sorted(
            mine,
            key=lambda c: (
                -sum(c.chunk.content.lower().split().count(t) for t in terms),
                order[c.document_id],
                c.chunk.ordinal,
            ),
        )[:k]
        return [self._scored(c, float(len(ranked) - i)) for i, c in enumerate(ranked)]

    async def recent_chunks(
        self, ws: WorkspaceId, principals: Principals, k: int, filters: SearchFilters
    ) -> list[ScoredChunk]:
        visible = self._visible(ws, principals, filters)
        epoch = datetime.min.replace(tzinfo=UTC)
        by_doc: dict[DocumentId, list[_StoredChunk]] = {}
        for c in visible:
            if c.document_id not in self.parents:  # attachments ride with their parent
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
            # Title and metadata match too, weighted above the body (P1-3, migration 0012).
            context = self.search_contexts.get(c.document_id, "").lower().split()
            score += 10.0 * sum(context.count(t) for t in terms)
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

    async def chunks_by_id(
        self, ws: WorkspaceId, principals: Principals, chunk_ids: Sequence[ChunkId]
    ) -> list[ScoredChunk]:
        wanted = set(chunk_ids)
        return [
            self._scored(c, 0.0)
            for c in self._visible(ws, principals, SearchFilters())
            if c.id in wanted
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

    # ---- attachments (ADR-0043)
    async def child_checksums(self, source_id: SourceId, parent_external_id: str) -> dict[str, str]:
        parent = self._by_key.get((source_id, parent_external_id))
        return {
            ext: self._docs[d][1].checksum
            for (src, ext), d in self._by_key.items()
            if src == source_id and parent is not None and self.parents.get(d) == parent
        }

    async def set_attachments(
        self, source_id: SourceId, parent_external_id: str, attachments: Sequence[AttachmentInfo]
    ) -> None:
        parent = self._by_key.get((source_id, parent_external_id))
        if parent is not None:
            meta = self.metadata.setdefault(parent, {})
            meta["attachments"] = [a.to_meta() for a in attachments]

    async def attachment_parent(
        self, ws: WorkspaceId, principals: Principals | None, doc_id: DocumentId
    ) -> AttachmentParent:
        found = self._docs.get(doc_id)
        if found is None or found[0] != ws or doc_id in self.parents:
            raise NotFound("document not found")
        doc = found[1]
        if principals is not None and not set(doc.acl) & set(principals.values):
            raise NotFound("document not found")
        return AttachmentParent(
            id=doc.id,
            workspace_id=ws,
            source_id=doc.source_id,
            connector=self._connector(doc),
            external_id=doc.external_id,
            title=doc.title,
            uri=doc.uri,
            author=doc.author,
            updated_at=doc.source_updated_at,
            data_class=self.data_classes.get(doc_id, "private"),
            acl=doc.acl,
            attachments=tuple(from_meta(self.metadata.get(doc_id, {}).get("attachments"))),
        )

    async def chunks_without_embedding(
        self, ws: WorkspaceId, doc_ids: Sequence[DocumentId], generation: GenerationId, limit: int
    ) -> list[ChunkToEmbed]:
        wanted = set(doc_ids)
        out: list[ChunkToEmbed] = []
        for c in sorted(self._chunks, key=lambda c: c.id):
            if c.workspace_id != ws or generation in c.embeddings:
                continue
            if c.document_id not in wanted and self.parents.get(c.document_id) not in wanted:
                continue
            doc = self._docs[c.document_id][1]
            out.append(
                ChunkToEmbed(
                    c.id, c.workspace_id, doc.title, c.chunk.heading_path, c.chunk.content,
                    c.chunk.ordinal,
                )
            )  # fmt: skip
            if len(out) == limit:
                break
        return out

    async def attachment_children(
        self, ws: WorkspaceId, principals: Principals, parents: Sequence[DocumentId]
    ) -> list[tuple[DocumentId, DocumentId]]:
        allowed = set(principals.values)
        wanted = set(parents)
        return [
            (child, parent)
            for child, parent in self.parents.items()
            if parent in wanted
            and self._docs[child][0] == ws
            and allowed & set(self._docs[child][1].acl)
        ]

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
