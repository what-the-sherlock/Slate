"""Retrieval metrics (docs/EVALS.md). Pure functions over ranked document lists.

Search returns chunks; metrics are computed on the ranked list of *unique documents*
(first appearance wins), because a citation points at a document and the per-document
cap means one document can occupy at most 3 slots anyway.

* Recall@k: share of the expected documents found in the top k (averaged over cases).
* MRR@k: 1 / rank of the first expected document in the top k (0 if none).
* nDCG@k: binary-relevance discounted gain, normalised by the ideal ordering.
"""

from __future__ import annotations

import math
from collections.abc import Hashable, Sequence


def unique_in_order[T: Hashable](items: Sequence[T]) -> list[T]:
    seen: set[T] = set()
    out: list[T] = []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def recall_at_k[T: Hashable](ranked: Sequence[T], expected: set[T], k: int) -> float:
    if not expected:
        raise ValueError("recall is undefined without expected documents")
    return len(set(ranked[:k]) & expected) / len(expected)


def mrr_at_k[T: Hashable](ranked: Sequence[T], expected: set[T], k: int) -> float:
    for i, doc in enumerate(ranked[:k], start=1):
        if doc in expected:
            return 1.0 / i
    return 0.0


def ndcg_at_k[T: Hashable](ranked: Sequence[T], expected: set[T], k: int) -> float:
    if not expected:
        raise ValueError("nDCG is undefined without expected documents")
    dcg = sum(1.0 / math.log2(i + 1) for i, d in enumerate(ranked[:k], start=1) if d in expected)
    ideal = sum(1.0 / math.log2(i + 1) for i in range(1, min(len(expected), k) + 1))
    return dcg / ideal


def percentile(values: Sequence[float], p: float) -> float:
    """Nearest-rank percentile (simple and exact for small samples)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[rank - 1]


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0
