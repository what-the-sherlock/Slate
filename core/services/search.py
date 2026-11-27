"""Hybrid search use case (ARCHITECTURE §8 steps 1, 3, 4).

Principals come from the session (never from the request). The keyword lane always
runs. The vector lane runs only when it can: there is a ``present`` generation built
by *this* embedder, the monthly budget isn't spent, and the embedder answers. If any
of these fail, search degrades to keyword-only and says so (``lanes``), rather than
failing the request or hiding the degradation.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import timedelta
from decimal import Decimal

import structlog

from core.domain.auth import Identity, Role
from core.domain.budget import SpendLimits
from core.domain.documents import DocumentPreview, ScoredChunk, SearchFilters
from core.domain.errors import RateLimited, UpstreamUnavailable, ValidationFailed
from core.domain.ids import ChunkId, DocumentId, GenerationId, WorkspaceId
from core.domain.principals import Principals
from core.domain.search import (
    MAX_CHUNKS_PER_DOCUMENT,
    MAX_QUERY_CHARS,
    FusedHit,
    Lane,
    RankedList,
    cap_per_document,
    rrf_fuse_lists,
)
from core.ports.models import Embedder
from core.ports.rate_limit import Limit, RateLimiter
from core.ports.storage import SearchIndex
from core.ports.usage import GenerationStore, UsageEntry, UsageLedger

log = structlog.get_logger(__name__)

CANDIDATES_PER_LANE = 50
# The recent lane (ADR-0031): the newest documents, weighted above one similarity list so
# "what's new" questions lead with new items, yet below the sum of both lanes for a query,
# so a recent document that also matches the question wins.
RECENT_DOCUMENTS = 20
RECENT_WEIGHT = 1.5
# The follow-up lane (P1-5, ADR-0038): the previous answer's cited documents, at the
# weight of an ordinary lane, so fresh evidence for the follow-up can still outrank them.
MAX_ANCHOR_DOCUMENTS = 8
ANCHOR_CANDIDATES = 20
ANCHOR_WEIGHT = 1.0
MAX_LIMIT = 50
MAX_AUTHOR_CHARS = 200
MAX_SOURCE_FILTERS = 20
PREVIEW_MAX_CHUNKS = 200
# Each search may spend a (cheap) paid query embedding and two index scans, so a user
# or a leaked session can't loop it freely. 30 a minute is far above human typing speed.
SEARCH_LIMIT = Limit(budget=30, window=timedelta(minutes=1))
# Daily too, so one account can't loop search all day (security review, ADR-0021).
SEARCH_DAILY_LIMIT = Limit(budget=1000, window=timedelta(days=1))
# Upper bound for one query embedding: 500 characters is ~150 tokens at $0.02 per million.
QUERY_EMBED_USD = Decimal("0.00001")
QUERY_USD_PER_M_TOKENS = Decimal("0.02")  # text-embedding-3-small


@dataclass(frozen=True, slots=True)
class SearchResults:
    hits: list[FusedHit]
    lanes: tuple[Lane, ...]  # the lanes that actually ran
    notices: tuple[str, ...] = field(default=())  # honest, user-facing degradations
    # Cosine similarity of the closest vector-lane passage, or None when the vector lane
    # didn't run. Fused scores are rank-only, so the answer gate needs this (ADR-0022).
    best_similarity: float | None = None


@dataclass(frozen=True, slots=True)
class VectorLane:
    embedder: Embedder
    generation: GenerationId


async def hybrid_retrieve(
    index: SearchIndex,
    ws: WorkspaceId,
    principals: Principals,
    query: str | Sequence[str],
    filters: SearchFilters,
    *,
    vector: VectorLane | None,
    candidates: int = CANDIDATES_PER_LANE,
    max_per_document: int = MAX_CHUNKS_PER_DOCUMENT,
    limit: int = 10,
    recent: bool = False,
    anchor: Sequence[DocumentId] = (),
) -> SearchResults:
    """The retrieval core shared by SearchService, chat and the eval harness, so evals
    measure exactly what users get: for each query, keyword || (embed -> vector); plus the
    recent lane when ``recent``; plus, for a follow-up, the anchor lane over ``anchor``
    (the previous answer's cited documents, P1-5); then weighted RRF and the
    per-document cap (ADR-0031).

    An embedder outage degrades to keyword-only with a notice instead of failing.
    """
    queries = (query,) if isinstance(query, str) else tuple(query)
    if not queries:
        raise ValueError("at least one query")

    async def vector_lane(q: str) -> tuple[list[ScoredChunk] | None, str | None]:
        if vector is None:
            return None, None
        try:
            qv = await vector.embedder.embed_query(q)
        except (UpstreamUnavailable, RateLimited) as exc:
            log.warning("search.vector_lane_unavailable", error=type(exc).__name__)
            return None, "Meaning-based search is unavailable right now; showing keyword matches."
        return await index.vector_search(
            ws, principals, qv, vector.generation, candidates, filters
        ), None

    # Structured concurrency: if one lane fails, the others are cancelled and awaited,
    # so no task outlives the request.
    async with asyncio.TaskGroup() as tg:
        keyword = [
            tg.create_task(index.keyword_search(ws, principals, q, candidates, filters))
            for q in queries
        ]
        vec = [tg.create_task(vector_lane(q)) for q in queries]
        newest = (
            tg.create_task(index.recent_chunks(ws, principals, RECENT_DOCUMENTS, filters))
            if recent
            else None
        )
        anchored = (
            tg.create_task(
                index.anchored_chunks(
                    ws,
                    principals,
                    tuple(anchor)[:MAX_ANCHOR_DOCUMENTS],
                    queries[0],
                    ANCHOR_CANDIDATES,
                    filters,
                )
            )
            if anchor
            else None
        )
    lists = [RankedList(Lane.KEYWORD, t.result()) for t in keyword]
    vector_lists = [hits for hits, _ in (t.result() for t in vec) if hits is not None]
    lists += [RankedList(Lane.VECTOR, hits) for hits in vector_lists]
    if newest is not None:
        lists.append(RankedList(Lane.RECENT, newest.result(), RECENT_WEIGHT))
    if anchored is not None:
        lists.append(RankedList(Lane.ANCHOR, anchored.result(), ANCHOR_WEIGHT))
    notices = tuple(dict.fromkeys(n for _, n in (t.result() for t in vec) if n))
    fused = rrf_fuse_lists(lists)
    hits = cap_per_document(fused, max_per_document=max_per_document)[:limit]
    lanes = tuple(dict.fromkeys(rl.lane for rl in lists))
    log.info(
        "search",
        lanes=[str(lane) for lane in lanes],
        queries=len(queries),
        candidates=sum(len(rl.hits) for rl in lists),
        returned=len(hits),
    )  # never log the query text: it can contain private content
    best = (
        max((h.score for hits in vector_lists for h in hits), default=None)
        if vector_lists
        else None
    )
    return SearchResults(hits, lanes, notices, best_similarity=best)


class SearchService:
    def __init__(
        self,
        index: SearchIndex,
        embedder: Embedder | None,
        generations: GenerationStore,
        ledger: UsageLedger,
        limiter: RateLimiter,
        *,
        limits: SpendLimits,
        candidates: int = CANDIDATES_PER_LANE,
        max_per_document: int = MAX_CHUNKS_PER_DOCUMENT,
    ) -> None:
        self._index = index
        self._embedder = embedder
        self._generations = generations
        self._ledger = ledger
        self._limiter = limiter
        self._limits = limits
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
        if f.author is not None and len(f.author.strip()) > MAX_AUTHOR_CHARS:
            raise ValidationFailed(
                f"The sender filter is limited to {MAX_AUTHOR_CHARS} characters."
            )
        if not await self._limiter.try_spend(f"search:{identity.user_id}", 1, SEARCH_LIMIT):
            raise RateLimited("Too many searches. Wait a moment and try again.", retry_after_s=60)
        if not await self._limiter.try_spend(
            f"search-day:{identity.user_id}", 1, SEARCH_DAILY_LIMIT
        ):
            raise RateLimited("You've reached today's search limit.", retry_after_s=86400)
        return await self.retrieve(identity, q, f, limit=limit)

    async def retrieve(
        self,
        identity: Identity,
        q: str | Sequence[str],
        f: SearchFilters,
        *,
        limit: int,
        recent: bool = False,
        anchor: Sequence[DocumentId] = (),
    ) -> SearchResults:
        """Validated query (or a plan's queries) -> hybrid retrieval, with spend checked and
        recorded. Shared by search and chat (no rate limit here: each caller applies its
        own)."""
        queries = (q,) if isinstance(q, str) else tuple(q)
        plan, notice = await self._vector_plan(
            identity.workspace_id,
            privileged=identity.is_platform_admin and identity.role is Role.OWNER,
        )
        res = await hybrid_retrieve(
            self._index,
            identity.workspace_id,
            identity.principals,
            queries,
            f,
            vector=plan,
            candidates=self._candidates,
            max_per_document=self._per_doc,
            limit=limit,
            recent=recent,
            anchor=anchor,
        )
        if plan is not None and Lane.VECTOR in res.lanes:
            for query in queries:
                await self._record_query_embedding(identity.workspace_id, query)
        return replace(res, notices=(notice, *res.notices)) if notice else res

    async def neighbours(
        self, identity: Identity, around: Sequence[tuple[DocumentId, int]]
    ) -> list[ScoredChunk]:
        """Chunks next to these, under the same ACL rule (SearchIndex.neighbours)."""
        return await self._index.neighbours(identity.workspace_id, identity.principals, around)

    async def _record_query_embedding(self, ws: WorkspaceId, q: str) -> None:
        """Every paid call is in the ledger, or the spend caps can't see it (ARCHITECTURE §12).
        Tokens are estimated (~4 characters each, rounded up); the data class is private
        because a query may be about private content (fail closed, ADR-0012)."""
        assert self._embedder is not None  # reason: only called when the vector lane ran
        tokens = len(q) // 4 + 1
        await self._ledger.record(
            UsageEntry(
                workspace_id=ws,
                provider="openai",
                model=self._embedder.model,
                kind="embed",
                data_class="private",
                tokens_in=tokens,
                tokens_out=0,
                cost_usd=QUERY_USD_PER_M_TOKENS * tokens / Decimal(1_000_000),
            )
        )

    async def _vector_plan(
        self, ws: WorkspaceId, *, privileged: bool
    ) -> tuple[VectorLane | None, str | None]:
        """Whether the vector lane may run, and with which embedder and generation."""
        if self._embedder is None:
            return None, None  # no embedding model configured (e.g. local dev without a key)
        gen = await self._generations.present()
        if gen is None or gen.model != self._embedder.model:
            return None, None  # nothing embedded with this model yet: keyword is all there is
        # A query embedding costs a fraction of a cent; refuse once any cap is reached.
        if reason := self._limits.refusal(
            await self._ledger.spend(ws), QUERY_EMBED_USD, privileged=privileged
        ):
            return None, f"Meaning-based search is paused. {reason}"
        return VectorLane(self._embedder, gen.id), None

    async def cited_chunks(
        self, identity: Identity, chunk_ids: Sequence[ChunkId]
    ) -> dict[ChunkId, ScoredChunk]:
        """The cited chunks this identity may still see, by id (reopened chats)."""
        found = await self._index.chunks_by_id(
            identity.workspace_id, identity.principals, list(dict.fromkeys(chunk_ids))
        )
        return {c.chunk_id: c for c in found}

    async def preview(self, identity: Identity, doc_id: DocumentId) -> DocumentPreview:
        """The document and its chunks, if this identity may see it. NotFound otherwise."""
        return await self._index.document_preview(
            identity.workspace_id, identity.principals, doc_id, PREVIEW_MAX_CHUNKS
        )
