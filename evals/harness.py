"""Run eval cases through the production retrieval core and score them.

``hybrid_retrieve`` is the exact function the Search API uses, so the numbers describe
what users get. Each case runs twice:

1. as the eval **owner** (workspace member): scored for Recall/MRR/nDCG;
2. as a **guest** (``user:<guest>`` + ``public`` only): any private document returned is
   a leak. The isolation target is exactly 0 (docs/EVALS.md).

The query vector is computed once per case and reused for the guest run.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from uuid import UUID

from core.domain.documents import SearchFilters, Vector
from core.domain.ids import DocumentId, UserId, WorkspaceId
from core.domain.principals import Principals
from core.domain.search import Lane
from core.ports.models import Embedder, Embeddings
from core.ports.storage import SearchIndex
from core.services.search import VectorLane, hybrid_retrieve
from evals.cases import Case
from evals.metrics import mean, mrr_at_k, ndcg_at_k, percentile, recall_at_k, unique_in_order

K = 10
# Fixed ids: principals are just strings, so the eval users needn't exist as rows.
EVAL_OWNER = UserId(UUID("00000000-0000-4000-8000-0000000e7a10"))
EVAL_GUEST = UserId(UUID("00000000-0000-4000-8000-0000000e7a11"))


class CachingEmbedder:
    """Memoises query vectors, so the guest run costs no second embedding call."""

    def __init__(self, inner: Embedder) -> None:
        self._inner = inner
        self._cache: dict[str, Vector] = {}
        self.calls = 0

    @property
    def model(self) -> str:
        return self._inner.model

    @property
    def dims(self) -> int:
        return self._inner.dims

    async def embed_documents(self, texts: Sequence[str]) -> Embeddings:
        return await self._inner.embed_documents(texts)

    async def embed_query(self, text: str) -> Vector:
        if text not in self._cache:
            self.calls += 1
            self._cache[text] = await self._inner.embed_query(text)
        return self._cache[text]


@dataclass(frozen=True, slots=True)
class CaseResult:
    id: str
    tags: tuple[str, ...]
    visibility: str
    answerable: bool
    top_documents: list[str]  # corpus paths, best first (unique)
    recall: float | None
    mrr: float | None
    ndcg: float | None
    latency_ms: float
    guest_leaks: list[str] = field(default_factory=list[str])


@dataclass(frozen=True, slots=True)
class Report:
    cases: list[CaseResult]
    summary: dict[str, object]

    def as_json(self) -> dict[str, object]:
        return {"summary": self.summary, "cases": [asdict(c) for c in self.cases]}


class DegradedRun(RuntimeError):
    """The vector lane didn't run although it was configured: numbers would mislead."""


async def run_cases(
    index: SearchIndex,
    ws: WorkspaceId,
    cases: Sequence[Case],
    doc_ids: Mapping[str, DocumentId],  # corpus path -> document id
    private_ids: set[DocumentId],
    *,
    vector: VectorLane | None,
    k: int = K,
) -> Report:
    path_of = {v: p for p, v in doc_ids.items()}
    owner = Principals.for_user(EVAL_OWNER, ws)
    guest = Principals.for_user(EVAL_GUEST, ws, is_guest=True)
    results: list[CaseResult] = []
    for case in cases:
        started = time.perf_counter()
        res = await hybrid_retrieve(
            index, ws, owner, case.question, SearchFilters(), vector=vector, limit=k
        )
        latency = (time.perf_counter() - started) * 1000
        if vector is not None and Lane.VECTOR not in res.lanes:
            raise DegradedRun(f"{case.id}: vector lane didn't run ({'; '.join(res.notices)})")
        ranked = unique_in_order([path_of.get(h.document_id, str(h.document_id)) for h in res.hits])
        expected = set(case.expected_documents)

        as_guest = await hybrid_retrieve(
            index, ws, guest, case.question, SearchFilters(), vector=vector, limit=k
        )
        leaks = sorted(
            {path_of.get(h.document_id, "?") for h in as_guest.hits if h.document_id in private_ids}
        )
        results.append(
            CaseResult(
                id=case.id,
                tags=case.tags,
                visibility=case.visibility,
                answerable=case.answerable,
                top_documents=ranked,
                recall=recall_at_k(ranked, expected, k) if expected else None,
                mrr=mrr_at_k(ranked, expected, k) if expected else None,
                ndcg=ndcg_at_k(ranked, expected, k) if expected else None,
                latency_ms=round(latency, 1),
                guest_leaks=leaks,
            )
        )
    return Report(results, summarise(results, k))


def summarise(results: Sequence[CaseResult], k: int = K) -> dict[str, object]:
    scored = [r for r in results if r.answerable]

    def block(rs: Sequence[CaseResult]) -> dict[str, object]:
        return {
            "cases": len(rs),
            f"recall@{k}": round(mean([r.recall or 0.0 for r in rs]), 4),
            f"mrr@{k}": round(mean([r.mrr or 0.0 for r in rs]), 4),
            f"ndcg@{k}": round(mean([r.ndcg or 0.0 for r in rs]), 4),
        }

    tags = sorted({t for r in scored for t in r.tags})
    latencies = [r.latency_ms for r in results]
    return {
        "overall": block(scored),
        "by_visibility": {
            v: block([r for r in scored if r.visibility == v]) for v in ("public", "private")
        },
        "by_tag": {t: block([r for r in scored if t in r.tags]) for t in tags},
        "isolation": {
            "guest_leaks": sum(len(r.guest_leaks) for r in results),
            "cases_with_leaks": [r.id for r in results if r.guest_leaks],
        },
        "latency_ms": {"p50": percentile(latencies, 50), "p95": percentile(latencies, 95)},
        "cases_total": len(results),
        "cases_no_answer": len(results) - len(scored),
    }
