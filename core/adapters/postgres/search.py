"""SearchIndex over Postgres: the ONLY place retrieval SQL lives (CLAUDE.md rule 1).

Both lanes filter ``workspace_id = :ws AND acl && :principals`` *inside* the query,
in the same scan that ranks rows. Nothing is post-filtered in Python, so a bug in the
caller can't widen what a user sees.

* **Vector lane:** cosine distance over ``chunk_embeddings`` for one generation, with
  ``hnsw.iterative_scan = relaxed_order`` so an ACL filter that hides most rows still
  fills k slots (ADR-0011). ``relaxed_order`` may return rows slightly out of order,
  so we re-sort by distance here.
* **Keyword lane:** ``websearch_to_tsquery`` (quotes, OR, -negation; never a syntax
  error on user input) over ``tsv_en`` (stemmed English) OR ``tsv_simple`` (unstemmed,
  for identifiers like ``ERR_BUDGET``). Each has its own GIN index; Postgres combines
  them with a BitmapOr.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from core.adapters.postgres.uow import UnitOfWork
from core.domain.documents import ScoredChunk, SearchFilters, Vector
from core.domain.ids import ChunkId, DocumentId, GenerationId, WorkspaceId
from core.domain.principals import Principals
from core.domain.search import MAX_QUERY_CHARS

DEFAULT_EF_SEARCH = 100
MAX_K = 200

_COLUMNS = "c.id, c.document_id, c.ordinal, c.content, c.heading_path, d.title"
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
    return "".join(f" AND {p}" for p in parts)


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
            " GREATEST(ts_rank_cd(c.tsv_en, qe), ts_rank_cd(c.tsv_simple, qs)) AS rank"
            " FROM chunks c"
            f" JOIN documents d ON d.id = c.document_id{_JOIN_SOURCE},"
            " websearch_to_tsquery('english', :q) qe,"
            " websearch_to_tsquery('simple', :q) qs"
            f" WHERE (c.tsv_en @@ qe OR c.tsv_simple @@ qs) AND {_VISIBLE}"
            f"{_filters_sql(filters, params)}"
            " ORDER BY rank DESC, c.id LIMIT :k"
        )
        async with self._uow() as s:
            rows = (await s.execute(text(sql), params)).mappings().all()
        return [_scored(r, float(r["rank"])) for r in rows]


def _scored(r: Any, score: float) -> ScoredChunk:  # reason: RowMapping values are Any
    return ScoredChunk(
        chunk_id=ChunkId(int(r["id"])),
        document_id=DocumentId(r["document_id"]),
        ordinal=int(r["ordinal"]),
        content=str(r["content"]),
        score=score,
        heading_path=r["heading_path"],
        title=r["title"],
    )
