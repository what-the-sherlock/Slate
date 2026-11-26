"""SearchIndex over Postgres: the ONLY place retrieval SQL lives (CLAUDE.md rule 1).

Both lanes filter ``workspace_id = :ws AND acl && :principals`` *inside* the query,
in the same scan that ranks rows. Nothing is post-filtered in Python, so a bug in the
caller can't widen what a user sees.

* **Vector lane:** cosine distance over ``chunk_embeddings`` for one generation, with
  ``hnsw.iterative_scan = relaxed_order`` so an ACL filter that hides most rows still
  fills k slots (ADR-0011). ``relaxed_order`` may return rows slightly out of order,
  so we re-sort by distance here.
* **Keyword lane:** over ``tsv_en`` (stemmed English) OR ``tsv_simple`` (unstemmed,
  for identifiers like ``ERR_BUDGET``). Each has its own GIN index; Postgres combines
  them with a BitmapOr. A plain question matches ANY of its words (lexemes OR-ed), and
  ``ts_rank_cd`` ranks passages matching more of them, and closer together, higher:
  ANDing every word made "what does KF-5002 mean" miss because "mean" isn't in the
  passage (measured: keyword Recall@10 0.70 with AND; see docs/EVALS.md). A query that
  uses search syntax (quotes, ``-word``, ``or``) gets ``websearch_to_tsquery``'s strict
  semantics instead. Neither form can raise a syntax error on user input.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from sqlalchemy import text

from core.adapters.postgres.uow import UnitOfWork
from core.domain.documents import (
    DocumentPreview,
    PreviewChunk,
    ScoredChunk,
    SearchFilters,
    Vector,
)
from core.domain.errors import NotFound
from core.domain.ids import ChunkId, DocumentId, GenerationId, SourceId, WorkspaceId
from core.domain.principals import Principals
from core.domain.search import MAX_QUERY_CHARS

DEFAULT_EF_SEARCH = 100
MAX_K = 200

_COLUMNS = "c.id, c.document_id, c.ordinal, c.content, c.heading_path, d.title, c.data_class"
# A source being deleted disappears from search at once, before cleanup removes its rows.
_VISIBLE = (
    "c.workspace_id = :ws AND c.acl && CAST(:p AS text[]) AND d.deleted_at IS NULL"
    " AND src.status <> 'deleting'"
)
_JOIN_SOURCE = " JOIN sources src ON src.id = d.source_id"


def _vector_literal(vector: Vector) -> str:
    return "[" + ",".join(f"{float(x):.7g}" for x in vector) + "]"


def _filters_sql(f: SearchFilters, params: dict[str, Any]) -> str:
    """Extra WHERE clauses. Only fixed SQL text is appended; values are bound."""
    parts: list[str] = []
    if f.source_ids:
        parts.append("d.source_id = ANY(CAST(:src AS uuid[]))")
        params["src"] = list(f.source_ids)
    if f.updated_after is not None:
        parts.append("d.source_updated_at >= :after")
        params["after"] = f.updated_after
    if f.updated_before is not None:
        parts.append("d.source_updated_at <= :before")
        params["before"] = f.updated_before
    if f.author:
        # A substring match; LIKE's wildcards in the user's text are escaped (bound value).
        parts.append("d.author ILIKE :author ESCAPE '\\'")
        escaped = f.author.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params["author"] = f"%{escaped}%"
    return "".join(f" AND {p}" for p in parts)


# Each lexeme of the query, OR-ed. to_tsvector normalises (and drops stop words) exactly
# as the stored columns did; quote_literal keeps lexemes like "kf-5002" whole, and the
# ::tsquery cast doesn't re-normalise them. Only stop words -> NULL -> no match.
_ANY_WORD = (
    "CAST((SELECT string_agg(quote_literal(l), ' | ') FROM"
    " unnest(tsvector_to_array(to_tsvector('{cfg}', :q))) AS l"
    # quote_literal emits E'..' for a backslash, which ::tsquery rejects: skip such
    # lexemes rather than fail the request (security review).
    " WHERE strpos(l, chr(92)) = 0) AS tsquery)"
)
_STRICT = "websearch_to_tsquery('{cfg}', :q)"
_SYNTAX = re.compile(r'"|(^|\s)-\S|\sor\s', re.IGNORECASE)


def _tsquery_sql(query: str, cfg: str, alias: str) -> str:
    """A FROM item ``(SELECT <tsquery> AS q) AS <alias>``. Returns constant SQL only:
    ``cfg`` and ``alias`` are literals from this module, never user input."""
    template = _STRICT if _SYNTAX.search(query) else _ANY_WORD
    return f"(SELECT {template.format(cfg=cfg)} AS q) AS {alias}"


def _check_k(k: int) -> int:
    if not 1 <= k <= MAX_K:
        raise ValueError(f"k must be between 1 and {MAX_K}")
    return k


class PostgresSearchIndex:
    def __init__(self, uow: UnitOfWork, *, ef_search: int = DEFAULT_EF_SEARCH) -> None:
        if ef_search < 1:
            raise ValueError("ef_search must be positive")
        self._uow = uow
        self._ef_search = ef_search

    async def vector_search(
        self,
        ws: WorkspaceId,
        principals: Principals,
        query_vector: Vector,
        generation: GenerationId,
        k: int,
        filters: SearchFilters,
    ) -> list[ScoredChunk]:
        k = _check_k(k)
        gen = int(generation)
        params: dict[str, Any] = {
            "ws": ws,
            "p": principals.as_list(),
            "q": _vector_literal(query_vector),
            "k": k,
        }
        # The generation id is inlined as a validated int, not bound: each generation has
        # a *partial* HNSW index (WHERE generation_id = N), and the planner can only use
        # it when it can prove the query's predicate matches, which a cached generic plan
        # with a parameter can't.
        sql = (
            # Only constants and a validated int are interpolated; all values are bound.
            f"SELECT {_COLUMNS}, e.embedding <=> CAST(:q AS halfvec) AS dist"  # noqa: S608
            " FROM chunk_embeddings e"
            " JOIN chunks c ON c.id = e.chunk_id"
            " JOIN documents d ON d.id = c.document_id"
            f"{_JOIN_SOURCE}"
            f" WHERE e.generation_id = {gen} AND {_VISIBLE}{_filters_sql(filters, params)}"
            " ORDER BY dist LIMIT :k"
        )
        ef = max(self._ef_search, k)  # HNSW returns at most ef_search rows per scan
        async with self._uow() as s:
            # SET LOCAL: scoped to this transaction, safe through PgBouncer. Ints only.
            await s.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
            await s.execute(text(f"SET LOCAL hnsw.ef_search = {int(ef)}"))
            rows = (await s.execute(text(sql), params)).mappings().all()
        hits = [_scored(r, 1.0 - float(r["dist"])) for r in rows]
        hits.sort(key=lambda h: (-h.score, h.chunk_id))  # undo relaxed_order
        return hits

    async def keyword_search(
        self,
        ws: WorkspaceId,
        principals: Principals,
        query: str,
        k: int,
        filters: SearchFilters,
    ) -> list[ScoredChunk]:
        k = _check_k(k)
        if len(query) > MAX_QUERY_CHARS:  # defence in depth; the service checks first
            raise ValueError(f"query longer than {MAX_QUERY_CHARS} characters")
        params: dict[str, Any] = {"ws": ws, "p": principals.as_list(), "q": query, "k": k}
        sql = (
            f"SELECT {_COLUMNS},"  # noqa: S608 (constant fragments only; values are bound)
            " GREATEST(ts_rank_cd(c.tsv_en, qe.q), ts_rank_cd(c.tsv_simple, qs.q)) AS rank"
            " FROM chunks c"
            f" JOIN documents d ON d.id = c.document_id{_JOIN_SOURCE},"
            f" {_tsquery_sql(query, 'english', 'qe')},"
            f" {_tsquery_sql(query, 'simple', 'qs')}"
            f" WHERE (c.tsv_en @@ qe.q OR c.tsv_simple @@ qs.q) AND {_VISIBLE}"
            f"{_filters_sql(filters, params)}"
            " ORDER BY rank DESC, c.id LIMIT :k"
        )
        async with self._uow() as s:
            rows = (await s.execute(text(sql), params)).mappings().all()
        return [_scored(r, float(r["rank"])) for r in rows]

    async def document_preview(
        self, ws: WorkspaceId, principals: Principals, doc_id: DocumentId, max_chunks: int
    ) -> DocumentPreview:
        params: dict[str, Any] = {"id": doc_id, "ws": ws, "p": principals.as_list()}
        async with self._uow() as s:
            doc = (await s.execute(text(_PREVIEW_DOC), params)).mappings().first()
            if doc is None:
                raise NotFound("document not found")  # same answer for missing and forbidden
            rows = (
                (await s.execute(text(_PREVIEW_CHUNKS), {**params, "n": _check_k(max_chunks)}))
                .mappings()
                .all()
            )
        return DocumentPreview(
            id=DocumentId(doc["id"]),
            source_id=SourceId(doc["source_id"]),
            source_name=str(doc["source_name"]),
            title=str(doc["title"]),
            uri=str(doc["uri"]),
            mime=str(doc["mime"]),
            source_updated_at=doc["source_updated_at"],
            chunks=tuple(
                PreviewChunk(int(r["ordinal"]), str(r["content"]), r["heading_path"]) for r in rows
            ),
        )

    async def neighbours(
        self, ws: WorkspaceId, principals: Principals, around: Sequence[tuple[DocumentId, int]]
    ) -> list[ScoredChunk]:
        if not around:
            return []
        if len(around) > MAX_K:
            raise ValueError(f"at most {MAX_K} passages")
        params: dict[str, Any] = {
            "ws": ws,
            "p": principals.as_list(),
            "docs": [d for d, _ in around],
            "ords": [o for _, o in around],
        }
        sql = (
            f"SELECT DISTINCT {_COLUMNS} FROM chunks c"  # noqa: S608 (constant fragments)
            f" JOIN documents d ON d.id = c.document_id{_JOIN_SOURCE}"
            " JOIN unnest(CAST(:docs AS uuid[]), CAST(:ords AS int[])) AS r(doc, ord)"
            "   ON c.document_id = r.doc AND c.ordinal IN (r.ord - 1, r.ord + 1)"
            f" WHERE {_VISIBLE}"
        )
        async with self._uow() as s:
            rows = (await s.execute(text(sql), params)).mappings().all()
        return [_scored(r, 0.0) for r in rows]


# Preview uses the SAME visibility rule as search (ARCHITECTURE §5), checked on the
# document row; chunks.acl is a trigger-maintained copy of documents.acl.
_PREVIEW_DOC = """
SELECT d.id, d.source_id, src.name AS source_name, d.title, d.uri, d.mime, d.source_updated_at
FROM documents d JOIN sources src ON src.id = d.source_id
WHERE d.id = :id AND d.workspace_id = :ws AND d.acl && CAST(:p AS text[])
  AND d.deleted_at IS NULL AND src.status <> 'deleting'
"""
_PREVIEW_CHUNKS = """
SELECT c.ordinal, c.content, c.heading_path FROM chunks c
WHERE c.document_id = :id AND c.workspace_id = :ws AND c.acl && CAST(:p AS text[])
ORDER BY c.ordinal LIMIT :n
"""


def _scored(r: Any, score: float) -> ScoredChunk:  # reason: RowMapping values are Any
    return ScoredChunk(
        chunk_id=ChunkId(int(r["id"])),
        document_id=DocumentId(r["document_id"]),
        ordinal=int(r["ordinal"]),
        content=str(r["content"]),
        score=score,
        heading_path=r["heading_path"],
        title=r["title"],
        data_class=str(r["data_class"]),
    )
