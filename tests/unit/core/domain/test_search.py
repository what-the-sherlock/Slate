from __future__ import annotations

from uuid import UUID

import pytest

from core.domain.documents import ScoredChunk
from core.domain.ids import ChunkId, DocumentId
from core.domain.search import (
    RRF_K,
    Lane,
    RankedList,
    cap_per_document,
    rrf_fuse,
    rrf_fuse_lists,
)

DOC_A = DocumentId(UUID(int=1))
DOC_B = DocumentId(UUID(int=2))


def hit(chunk: int, doc: DocumentId = DOC_A, score: float = 1.0) -> ScoredChunk:
    return ScoredChunk(ChunkId(chunk), doc, chunk, f"c{chunk}", score)


def test_rrf_score_is_sum_of_reciprocal_ranks() -> None:
    fused = rrf_fuse({Lane.VECTOR: [hit(1), hit(2)], Lane.KEYWORD: [hit(2), hit(3)]})
    by_id = {h.chunk_id: h for h in fused}
    assert by_id[ChunkId(2)].score == pytest.approx(1 / (RRF_K + 2) + 1 / (RRF_K + 1))
    assert by_id[ChunkId(1)].score == pytest.approx(1 / (RRF_K + 1))
    assert by_id[ChunkId(2)].ranks == {Lane.VECTOR: 2, Lane.KEYWORD: 1}
    # Found by both lanes beats rank 1 in only one lane.
    assert [h.chunk_id for h in fused] == [2, 1, 3]


def test_rrf_ignores_raw_scores() -> None:
    # A huge keyword score must not beat a chunk both lanes agree on.
    fused = rrf_fuse(
        {
            Lane.KEYWORD: [hit(9, score=1e9), hit(1, score=0.01)],
            Lane.VECTOR: [hit(1, score=0.2)],
        }
    )
    assert fused[0].chunk_id == 1


def test_ties_break_deterministically() -> None:
    fused = rrf_fuse({Lane.VECTOR: [hit(5)], Lane.KEYWORD: [hit(4)]})
    assert [h.chunk_id for h in fused] == [4, 5]  # same score and rank: lower id first


def test_duplicate_within_a_lane_counts_once() -> None:
    fused = rrf_fuse({Lane.VECTOR: [hit(1), hit(1), hit(2)]})
    assert fused[0].score == pytest.approx(1 / (RRF_K + 1))
    assert fused[1].ranks == {Lane.VECTOR: 3}


def test_empty_lanes() -> None:
    assert rrf_fuse({}) == []
    assert rrf_fuse({Lane.VECTOR: [], Lane.KEYWORD: []}) == []


def test_cap_per_document_keeps_order() -> None:
    hits = rrf_fuse({Lane.VECTOR: [hit(1), hit(2), hit(3), hit(4), hit(5, DOC_B), hit(6)]})
    capped = cap_per_document(hits, max_per_document=2)
    assert [h.chunk_id for h in capped] == [1, 2, 5]


def test_invalid_parameters() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        rrf_fuse({}, k=-1)
    with pytest.raises(ValueError, match="at least 1"):
        cap_per_document([], max_per_document=0)


def test_weighted_lists_fuse_several_queries_and_keep_each_lanes_best_rank() -> None:
    fused = rrf_fuse_lists(
        [
            RankedList(Lane.KEYWORD, [hit(1), hit(2)]),
            RankedList(Lane.KEYWORD, [hit(2)]),  # a second query's keyword list
            RankedList(Lane.RECENT, [hit(3)], weight=2.0),
        ]
    )
    by_id = {h.chunk_id: h for h in fused}
    assert by_id[ChunkId(2)].score == pytest.approx(1 / (RRF_K + 2) + 1 / (RRF_K + 1))
    assert by_id[ChunkId(2)].ranks == {Lane.KEYWORD: 1}
    assert by_id[ChunkId(3)].score == pytest.approx(2 / (RRF_K + 1))
    assert [h.chunk_id for h in fused] == [3, 2, 1]


def test_weighted_lists_reject_negative_weights() -> None:
    with pytest.raises(ValueError, match="weights"):
        rrf_fuse_lists([RankedList(Lane.KEYWORD, [hit(1)], weight=-1)])


def test_rrf_fuse_equals_unit_weight_lists() -> None:
    lanes = {Lane.VECTOR: [hit(1), hit(2)], Lane.KEYWORD: [hit(2), hit(3)]}
    as_lists = rrf_fuse_lists([RankedList(lane, hits) for lane, hits in lanes.items()])
    assert rrf_fuse(lanes) == as_lists
