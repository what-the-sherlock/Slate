"""Source diversity for cross-source questions (P1-7): a pure selection over fused hits."""

from __future__ import annotations

from uuid import UUID

from core.domain.documents import ScoredChunk
from core.domain.ids import ChunkId, DocumentId
from core.domain.search import FusedHit, Lane, diversify


def fused(n: int, connector: str, score: float) -> FusedHit:
    chunk = ScoredChunk(ChunkId(n), DocumentId(UUID(int=n)), 0, f"c{n}", score, connector=connector)
    return FusedHit(chunk, score, {Lane.KEYWORD: n})


def ids(hits: list[FusedHit]) -> list[int]:
    return [int(h.chunk_id) for h in hits]


def test_a_strong_hit_from_a_missing_connector_joins_after_the_top_three() -> None:
    pool = [fused(i, "upload", 1.0 - i * 0.01) for i in range(1, 7)]
    pool += [fused(20, "email", 0.6), fused(21, "email", 0.55), fused(30, "github", 0.58)]
    got = diversify(pool, 5)
    assert len(got) == 5
    assert ids(got) == [1, 2, 3, 20, 30]  # email's best and github's best, ahead of the tail


def test_a_weak_hit_does_not_displace_a_relevant_one() -> None:
    pool = [fused(i, "upload", 1.0) for i in range(1, 5)] + [fused(9, "discord", 0.4)]
    assert ids(diversify(pool, 3)) == [1, 2, 3]  # 0.4 < half of 1.0


def test_only_a_connectors_best_hit_is_considered() -> None:
    pool = [fused(1, "upload", 1.0), fused(2, "upload", 0.9), fused(3, "email", 0.3)]
    pool += [fused(4, "email", 0.8)]  # lower in the pool than email's best, which is weak
    assert ids(diversify(pool, 2)) == [1, 2]


def test_nothing_changes_when_the_top_already_has_every_connector() -> None:
    pool = [fused(1, "upload", 1.0), fused(2, "email", 0.9), fused(3, "upload", 0.8)]
    assert ids(diversify(pool, 2)) == [1, 2]
    assert diversify([], 5) == []
