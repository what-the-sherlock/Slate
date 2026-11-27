"""Hybrid-search fusion rules (ARCHITECTURE §8 steps 3-4). Pure: no I/O.

Reciprocal rank fusion (RRF) merges ranked lists using only *ranks*, so a cosine
similarity and a full-text rank never need to share a scale:

    score(chunk) = sum over lanes of 1 / (K + rank_in_lane)       (rank starts at 1)

K = 60 is the constant from the original RRF paper (Cormack et al., 2009). It damps the
gap between rank 1 and rank 2, so one lane can't dominate on a single strong hit.

A planned question (ADR-0031) produces several ranked lists: both lanes for each of its
queries, plus the recent lane for "latest" questions. ``rrf_fuse_lists`` fuses any
number of lists, each with a weight (weighted RRF): ``sum of weight / (K + rank)``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from core.domain.documents import ScoredChunk
from core.domain.ids import ChunkId, DocumentId

RRF_K = 60
MAX_CHUNKS_PER_DOCUMENT = 3
MAX_QUERY_CHARS = 500


class Lane(StrEnum):
    VECTOR = "vector"
    KEYWORD = "keyword"
    RECENT = "recent"  # newest documents first (ADR-0031)
    # The previous answer's cited documents, ranked by the follow-up (P1-5, ADR-0038).
    ANCHOR = "anchor"


@dataclass(frozen=True, slots=True)
class RankedList:
    """One lane's results for one query, best first, with its RRF weight."""

    lane: Lane
    hits: Sequence[ScoredChunk]
    weight: float = 1.0


@dataclass(frozen=True, slots=True)
class FusedHit:
    chunk: ScoredChunk
    score: float
    ranks: Mapping[Lane, int]  # 1-based rank in each lane that found this chunk

    @property
    def chunk_id(self) -> ChunkId:
        return self.chunk.chunk_id

    @property
    def document_id(self) -> DocumentId:
        return self.chunk.document_id


def rrf_fuse(lanes: Mapping[Lane, Sequence[ScoredChunk]], *, k: int = RRF_K) -> list[FusedHit]:
    """Fuse ranked lanes, deduplicating by chunk. Each lane must already be best-first.

    Ties break by best single-lane rank, then by chunk id, so output is deterministic.
    """
    return rrf_fuse_lists([RankedList(lane, hits) for lane, hits in lanes.items()], k=k)


def rrf_fuse_lists(lists: Sequence[RankedList], *, k: int = RRF_K) -> list[FusedHit]:
    """Weighted RRF over any number of ranked lists (several per lane are allowed).

    ``ranks`` keeps each lane's best rank for a chunk. Raises ValueError on a negative
    ``k`` or weight.
    """
    if k < 0:
        raise ValueError("k must be non-negative")
    scores: dict[ChunkId, float] = {}
    ranks: dict[ChunkId, dict[Lane, int]] = {}
    chunks: dict[ChunkId, ScoredChunk] = {}
    for ranked in lists:
        if ranked.weight < 0:
            raise ValueError("weights must be non-negative")
        seen: set[ChunkId] = set()
        for rank, hit in enumerate(ranked.hits, start=1):
            if hit.chunk_id in seen:  # a list naming a chunk twice counts it once
                continue
            seen.add(hit.chunk_id)
            scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + ranked.weight / (k + rank)
            best = ranks.setdefault(hit.chunk_id, {})
            best[ranked.lane] = min(rank, best.get(ranked.lane, rank))
            chunks.setdefault(hit.chunk_id, hit)
    fused = [FusedHit(chunks[c], scores[c], ranks[c]) for c in scores]
    fused.sort(key=lambda h: (-h.score, min(h.ranks.values()), h.chunk_id))
    return fused


def cap_per_document(
    hits: Sequence[FusedHit], *, max_per_document: int = MAX_CHUNKS_PER_DOCUMENT
) -> list[FusedHit]:
    """Keep at most N chunks per document, preserving order, so one long document
    can't crowd every other source out of the results."""
    if max_per_document < 1:
        raise ValueError("max_per_document must be at least 1")
    counts: dict[DocumentId, int] = {}
    out: list[FusedHit] = []
    for h in hits:
        n = counts.get(h.document_id, 0)
        if n < max_per_document:
            counts[h.document_id] = n + 1
            out.append(h)
    return out


# Source diversity (P1-7, ADR-0039): a cross-source question needs more than the single
# connector that happens to rank highest.
DIVERSITY_POOL = 50  # fused hits considered
DIVERSITY_MIN_RATIO = 0.5  # of the top hit's fused score
DIVERSITY_SLOT = 3  # extras go right after this many top hits


def diversify(
    hits: Sequence[FusedHit],
    limit: int,
    *,
    min_ratio: float = DIVERSITY_MIN_RATIO,
    slot: int = DIVERSITY_SLOT,
) -> list[FusedHit]:
    """The top ``limit`` hits, plus the best hit of every connector that found something
    in the pool but not in the top, when it scores at least ``min_ratio`` of the best.
    A light form of MMR (maximal marginal relevance): relevance first, then coverage.

    Extras replace the lowest-ranked hits and are placed after the first ``slot`` hits,
    not at the end, because the token budget cuts from the end."""
    if not hits or limit < 1:
        return []
    top = list(hits[:limit])
    seen = {h.chunk.connector for h in top}
    bar = hits[0].score * min_ratio
    extras: list[FusedHit] = []
    for h in hits[limit:]:
        connector = h.chunk.connector
        if connector is None or connector in seen:
            continue
        seen.add(connector)  # only a connector's best hit is considered
        if h.score >= bar:
            extras.append(h)
    extras = extras[: max(0, limit - slot)]
    if not extras:
        return top
    keep = top[: limit - len(extras)]
    return keep[:slot] + extras + keep[slot:]
