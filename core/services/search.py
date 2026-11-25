"""Hybrid search use case (ARCHITECTURE §8 steps 1, 3, 4).

Principals come from the session (never from the request). The keyword lane always
runs. The vector lane runs only when it can: there is a ``present`` generation built
by *this* embedder, the monthly budget isn't spent, and the embedder answers. If any
of these fail, search degrades to keyword-only and says so (``lanes``), rather than
failing the request or hiding the degradation.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import timedelta
from decimal import Decimal

import structlog

from core.domain.auth import Identity
from core.domain.documents import DocumentPreview, ScoredChunk, SearchFilters
from core.domain.errors import RateLimited, UpstreamUnavailable, ValidationFailed
from core.domain.ids import DocumentId, GenerationId, WorkspaceId
from core.domain.principals import Principals
from core.domain.search import (
    MAX_CHUNKS_PER_DOCUMENT,
    MAX_QUERY_CHARS,
    FusedHit,
    Lane,
    cap_per_document,
    rrf_fuse,
)
from core.ports.models import Embedder
from core.ports.rate_limit import Limit, RateLimiter
from core.ports.storage import SearchIndex
from core.ports.usage import GenerationStore, UsageLedger

log = structlog.get_logger(__name__)

CANDIDATES_PER_LANE = 50
MAX_LIMIT = 50
MAX_SOURCE_FILTERS = 20
PREVIEW_MAX_CHUNKS = 200
# Each search may spend a (cheap) paid query embedding and two index scans, so a user
# or a leaked session can't loop it freely. 60 a minute is far above human typing speed.
SEARCH_LIMIT = Limit(budget=60, window=timedelta(minutes=1))


@dataclass(frozen=True, slots=True)
class SearchResults:
    hits: list[FusedHit]
    lanes: tuple[Lane, ...]  # the lanes that actually ran
    notices: tuple[str, ...] = field(default=())  # honest, user-facing degradations


@dataclass(frozen=True, slots=True)
class VectorLane:
    embedder: Embedder
    generation: GenerationId


async def hybrid_retrieve(
    index: SearchIndex,
    ws: WorkspaceId,
    principals: Principals,
    query: str,
    filters: SearchFilters,
    *,
    vector: VectorLane | None,
    candidates: int = CANDIDATES_PER_LANE,
    max_per_document: int = MAX_CHUNKS_PER_DOCUMENT,
    limit: int = 10,
) -> SearchResults:
    """The retrieval core shared by SearchService and the eval harness, so evals measure
    exactly what users get: keyword || (embed -> vector), then RRF and the per-doc cap.

    An embedder outage degrades to keyword-only with a notice instead of failing.
    """

    async def vector_lane() -> tuple[list[ScoredChunk] | None, str | None]:
        if vector is None:
            return None, None
        try:
            qv = await vector.embedder.embed_query(query)
        except (UpstreamUnavailable, RateLimited) as exc:
            log.warning("search.vector_lane_unavailable", error=type(exc).__name__)
            return None, "Meaning-based search is unavailable right now; showing keyword matches."
        return (
            await index.vector_search(ws, principals, qv, vector.generation, candidates, filters),
            None,
        )

    # Structured concurrency: if one lane fails, the other is cancelled and awaited,
    # so no task outlives the request.
    async with asyncio.TaskGroup() as tg:
        keyword = tg.create_task(index.keyword_search(ws, principals, query, candidates, filters))
        vec = tg.create_task(vector_lane())
    vector_hits, notice = vec.result()
    lanes: dict[Lane, list[ScoredChunk]] = {Lane.KEYWORD: keyword.result()}
    if vector_hits is not None:
        lanes[Lane.VECTOR] = vector_hits
    hits = cap_per_document(rrf_fuse(lanes), max_per_document=max_per_document)[:limit]
    log.info(
        "search",
        lanes=[str(lane) for lane in lanes],
        candidates={str(k): len(v) for k, v in lanes.items()},
        returned=len(hits),
    )  # never log the query text: it can contain private content
    return SearchResults(hits, tuple(lanes), (notice,) if notice else ())


class SearchService:
    def __init__(
        self,
        index: SearchIndex,
        embedder: Embedder | None,
        generations: GenerationStore,
        ledger: UsageLedger,
        limiter: RateLimiter,
        *,
        monthly_budget_usd: Decimal,
        candidates: int = CANDIDATES_PER_LANE,
        max_per_document: int = MAX_CHUNKS_PER_DOCUMENT,
    ) -> None:
        self._index = index
        self._embedder = embedder
        self._generations = generations
        self._ledger = ledger
        self._limiter = limiter
        self._budget = monthly_budget_usd
        self._candidates = candidates
        self._per_doc = max_per_document

    async def search(
        self,
        identity: Identity,
        query: str,
        *,
        filters: SearchFilters | None = None,
        limit: int = 10,
    ) -> SearchResults:
        q = " ".join(query.split())  # collapse whitespace; also strips
        if not q:
            raise ValidationFailed("Type something to search for.")
        if len(q) > MAX_QUERY_CHARS:
            raise ValidationFailed(f"Search is limited to {MAX_QUERY_CHARS} characters.")
        limit = max(1, min(limit, MAX_LIMIT))
        f = filters or SearchFilters()
        if len(f.source_ids) > MAX_SOURCE_FILTERS:
            raise ValidationFailed(f"Filter by at most {MAX_SOURCE_FILTERS} sources.")
        if not await self._limiter.try_spend(f"search:{identity.user_id}", 1, SEARCH_LIMIT):
            raise RateLimited("Too many searches. Wait a moment and try again.", retry_after_s=60)
        plan, notice = await self._vector_plan()
        res = await hybrid_retrieve(
            self._index,
            identity.workspace_id,
            identity.principals,
            q,
            f,
            vector=plan,
            candidates=self._candidates,
            max_per_document=self._per_doc,
            limit=limit,
        )
        return replace(res, notices=(notice, *res.notices)) if notice else res

    async def _vector_plan(self) -> tuple[VectorLane | None, str | None]:
        """Whether the vector lane may run, and with which embedder and generation."""
        if self._embedder is None:
            return None, None  # no embedding model configured (e.g. local dev without a key)
        gen = await self._generations.present()
        if gen is None or gen.model != self._embedder.model:
            return None, None  # nothing embedded with this model yet: keyword is all there is
        if await self._ledger.month_to_date_usd() >= self._budget:
            return None, "Meaning-based search is paused: this month's AI budget is used up."
        return VectorLane(self._embedder, gen.id), None

    async def preview(self, identity: Identity, doc_id: DocumentId) -> DocumentPreview:
        """The document and its chunks, if this identity may see it. NotFound otherwise."""
        return await self._index.document_preview(
            identity.workspace_id, identity.principals, doc_id, PREVIEW_MAX_CHUNKS
        )
