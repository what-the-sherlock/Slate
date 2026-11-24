"""Hybrid-search fusion rules (ARCHITECTURE §8 steps 3-4). Pure: no I/O.

Reciprocal rank fusion (RRF) merges ranked lists using only *ranks*, so a cosine
similarity and a full-text rank never need to share a scale:

    score(chunk) = sum over lanes of 1 / (K + rank_in_lane)       (rank starts at 1)

K = 60 is the constant from the original RRF paper (Cormack et al., 2009). It damps the
gap between rank 1 and rank 2, so one lane can't dominate on a single strong hit.
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
    if k < 0:
        raise ValueError("k must be non-negative")
    scores: dict[ChunkId, float] = {}
    ranks: dict[ChunkId, dict[Lane, int]] = {}
    chunks: dict[ChunkId, ScoredChunk] = {}
    for lane, hits in lanes.items():
        seen: set[ChunkId] = set()
        for rank, hit in enumerate(hits, start=1):
            if hit.chunk_id in seen:  # a lane listing a chunk twice counts it once
                continue
            seen.add(hit.chunk_id)
            scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + 1.0 / (k + rank)
            ranks.setdefault(hit.chunk_id, {})[lane] = rank
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
