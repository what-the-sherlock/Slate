"""DocumentStore over Postgres.

``write_document`` does the whole ingestion write in ONE transaction: upsert the
document, replace its chunks, insert their embeddings. Search can never observe a
document with half its chunks or chunks without vectors.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core.adapters.postgres.uow import UnitOfWork
from core.domain.documents import Document, DocumentUpsert, NewChunk, Vector
from core.domain.errors import NotFound
from core.domain.ids import DocumentId, GenerationId, SourceId, WorkspaceId
from core.domain.principals import Principals


def _vector_literal(vector: Vector) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in vector) + "]"


class PostgresDocumentStore:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def write_document(
        self,
        ws: WorkspaceId,
        doc: DocumentUpsert,
        chunks: Sequence[NewChunk],
        embeddings: Mapping[GenerationId, Sequence[Vector]],
        *,
        data_class: str,
    ) -> tuple[DocumentId, bool]:
        for gen, vectors in embeddings.items():
            if len(vectors) != len(chunks):
                raise ValueError(f"generation {gen}: {len(vectors)} vectors, {len(chunks)} chunks")
        async with self._uow() as s:
            doc_id, changed = await self._upsert(s, ws, doc, data_class)
            if not changed:
                return doc_id, False
            await s.execute(text("DELETE FROM chunks WHERE document_id = :d"), {"d": doc_id})
            if not chunks:
                return doc_id, True
            rows = (
                await s.execute(
                    text(
                        "INSERT INTO chunks (document_id, workspace_id, ordinal, heading_path,"
                        " content, token_count, acl, data_class)"
                        " SELECT :d, :ws, o, h, c, t, '{}', :dc"
                        " FROM unnest(CAST(:o AS int[]), CAST(:h AS text[]),"
                        "   CAST(:c AS text[]), CAST(:t AS int[])) AS x(o, h, c, t)"
                        " RETURNING id, ordinal"
                    ),
                    {
                        "d": doc_id,
                        "ws": ws,
                        "dc": data_class,
                        "o": [c.ordinal for c in chunks],
                        "h": [c.heading_path for c in chunks],
                        "c": [c.content for c in chunks],
                        "t": [c.token_count for c in chunks],
                    },
                )
            ).all()
            by_ordinal = {int(r[1]): int(r[0]) for r in rows}
            for gen, vectors in embeddings.items():
                await s.execute(
                    text(
                        "INSERT INTO chunk_embeddings (chunk_id, generation_id, embedding)"
                        " SELECT ch, :g, CAST(v AS halfvec(1024))"
                        " FROM unnest(CAST(:ch AS bigint[]), CAST(:v AS text[])) AS x(ch, v)"
                    ),
                    {
                        "g": gen,
                        "ch": [by_ordinal[c.ordinal] for c in chunks],
                        "v": [_vector_literal(v) for v in vectors],
                    },
                )
            return doc_id, True

    async def _upsert(
        self, s: AsyncSession, ws: WorkspaceId, doc: DocumentUpsert, data_class: str
    ) -> tuple[DocumentId, bool]:
        existing = (
            await s.execute(
                text(
                    "SELECT id, checksum FROM documents"
                    " WHERE source_id = :s AND external_id = :e FOR UPDATE"
                ),
                {"s": doc.source_id, "e": doc.external_id},
            )
        ).first()
        params = {
            "ws": ws,
            "s": doc.source_id,
            "e": doc.external_id,
            "uri": doc.uri,
            "title": doc.title,
            "mime": doc.mime,
            "author": doc.author,
            "sum": doc.checksum,
            "acl": list(doc.acl),
            "dc": data_class,
            "meta": json.dumps(dict(doc.metadata)),
            "sc": doc.source_created_at,
            "su": doc.source_updated_at,
        }
        if existing is None:
            new_id = (
                await s.execute(
                    text(
                        "INSERT INTO documents (workspace_id, source_id, external_id, uri, title,"
                        " mime, author, checksum, acl, data_class, metadata, source_created_at,"
                        " source_updated_at) VALUES (:ws, :s, :e, :uri, :title, :mime, :author,"
                        " :sum, CAST(:acl AS text[]), :dc, CAST(:meta AS jsonb), :sc, :su)"
                        " RETURNING id"
                    ),
                    params,
                )
            ).scalar_one()
            return DocumentId(new_id), True
        doc_id = DocumentId(existing[0])
        changed = existing[1] != doc.checksum
        # Always refresh metadata and ACL (a visibility change rewrites chunk ACLs via
        # trigger without re-embedding); bump the version only when content changed.
        await s.execute(
            text(
                "UPDATE documents SET uri = :uri, title = :title, mime = :mime, author = :author,"
                " checksum = :sum, acl = CAST(:acl AS text[]), metadata = CAST(:meta AS jsonb),"
                " source_updated_at = :su, deleted_at = NULL, updated_at = now(),"
                " version = version + CASE WHEN checksum <> :sum THEN 1 ELSE 0 END"
                " WHERE id = :id"
            ),
            {**params, "id": doc_id},
        )
        return doc_id, changed

    async def upsert_document(self, ws: WorkspaceId, doc: DocumentUpsert) -> DocumentId:
        async with self._uow() as s:
            doc_id, _ = await self._upsert(s, ws, doc, "private")
            return doc_id

    async def replace_chunks(
        self,
        ws: WorkspaceId,
        doc_id: DocumentId,
        chunks: Sequence[NewChunk],
        embeddings: Mapping[GenerationId, Sequence[Vector]],
    ) -> None:
        raise NotImplementedError("use write_document (one transaction)")

    async def get_document(
        self, ws: WorkspaceId, doc_id: DocumentId, principals: Principals
    ) -> Document:
        async with self._uow() as s:
            row = (
                (
                    await s.execute(
                        text(
                            "SELECT id, source_id, external_id, uri, title, mime, checksum,"
                            " version, acl, author, source_updated_at FROM documents"
                            " WHERE id = :id AND workspace_id = :ws AND deleted_at IS NULL"
                            " AND acl && CAST(:p AS text[])"
                        ),
                        {"id": doc_id, "ws": ws, "p": principals.as_list()},
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise NotFound("document not found")  # same answer for missing and forbidden
        return Document(
            id=DocumentId(row["id"]),
            source_id=SourceId(row["source_id"]),
            external_id=row["external_id"],
            uri=row["uri"],
            title=row["title"],
            mime=row["mime"],
            checksum=row["checksum"],
            version=row["version"],
            acl=tuple(row["acl"]),
            author=row["author"],
            source_updated_at=row["source_updated_at"],
        )
