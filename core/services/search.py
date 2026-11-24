"""Hybrid search use case (ARCHITECTURE §8 steps 1, 3, 4).

Principals come from the session (never from the request). The keyword lane always
runs. The vector lane runs only when it can: there is a ``present`` generation built
by *this* embedder, the monthly budget isn't spent, and the embedder answers. If any
of these fail, search degrades to keyword-only and says so (``lanes``), rather than
failing the request or hiding the degradation.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal

import structlog

from core.domain.auth import Identity
from core.domain.documents import ScoredChunk, SearchFilters
from core.domain.errors import RateLimited, UpstreamUnavailable, ValidationFailed
from core.domain.search import (
    MAX_CHUNKS_PER_DOCUMENT,
    MAX_QUERY_CHARS,
    FusedHit,
    Lane,
    cap_per_document,
    rrf_fuse,
)
from core.ports.models import Embedder
from core.ports.storage import SearchIndex
from core.ports.usage import GenerationStore, UsageLedger

log = structlog.get_logger(__name__)

CANDIDATES_PER_LANE = 50
MAX_LIMIT = 50


@dataclass(frozen=True, slots=True)
class SearchResults:
    hits: list[FusedHit]
    lanes: tuple[Lane, ...]  # the lanes that actually ran
    notices: tuple[str, ...] = field(default=())  # honest, user-facing degradations


class SearchService:
    def __init__(
        self,
        index: SearchIndex,
        embedder: Embedder,
        generations: GenerationStore,
        ledger: UsageLedger,
        *,
        monthly_budget_usd: Decimal,
        candidates: int = CANDIDATES_PER_LANE,
        max_per_document: int = MAX_CHUNKS_PER_DOCUMENT,
    ) -> None:
        self._index = index
        self._embedder = embedder
        self._generations = generations
        self._ledger = ledger
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
        ws, principals = identity.workspace_id, identity.principals

        # Structured concurrency: if one lane fails, the other is cancelled and awaited,
        # so no task outlives the request.
        async with asyncio.TaskGroup() as tg:
            keyword = tg.create_task(
                self._index.keyword_search(ws, principals, q, self._candidates, f)
            )
            vector = tg.create_task(self._vector_lane(identity, q, f))
        vector_hits, notice = vector.result()
        lanes: dict[Lane, list[ScoredChunk]] = {Lane.KEYWORD: keyword.result()}
        if vector_hits is not None:
            lanes[Lane.VECTOR] = vector_hits

        hits = cap_per_document(rrf_fuse(lanes), max_per_document=self._per_doc)[:limit]
        log.info(
            "search",
            lanes=[str(lane) for lane in lanes],
            candidates={str(k): len(v) for k, v in lanes.items()},
            returned=len(hits),
        )  # never log the query text: it can contain private content
        return SearchResults(hits, tuple(lanes), (notice,) if notice else ())

    async def _vector_lane(
        self, identity: Identity, q: str, f: SearchFilters
    ) -> tuple[list[ScoredChunk] | None, str | None]:
        gen = await self._generations.present()
        if gen is None or gen.model != self._embedder.model:
            return None, None  # nothing embedded with this model yet: keyword is all there is
        if await self._ledger.month_to_date_usd() >= self._budget:
            return None, "Meaning-based search is paused: this month's AI budget is used up."
        try:
            vector = await self._embedder.embed_query(q)
        except (UpstreamUnavailable, RateLimited) as exc:
            log.warning("search.vector_lane_unavailable", error=type(exc).__name__)
            return None, "Meaning-based search is unavailable right now; showing keyword matches."
        hits = await self._index.vector_search(
            identity.workspace_id, identity.principals, vector, gen.id, self._candidates, f
        )
        return hits, None
