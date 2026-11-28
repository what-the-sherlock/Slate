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
from datetime import UTC
from uuid import UUID

from core.domain.answer import DEFAULT_MIN_SIMILARITY
from core.domain.documents import SearchFilters, Vector
from core.domain.ids import DocumentId, UserId, WorkspaceId
from core.domain.planner import merge_filters
from core.domain.principals import Principals
from core.domain.search import Lane
from core.ports.models import Embedder, Embeddings
from core.ports.storage import SearchIndex
from core.services.search import VectorLane, hybrid_retrieve
from evals.cases import EVAL_NOW, Case
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
    # Closest vector match (cosine similarity); None without the vector lane. Feeds the
    # answer gate's threshold (ADR-0022).
    best_similarity: float | None = None


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
    # An outsider: entitled to public content only (the guest login is gone, ADR-0021,
    # but the leak check is unchanged: any private document they get is a leak).
    guest = Principals.for_outsider(EVAL_GUEST)
    results: list[CaseResult] = []
    for case in cases:
        # An oracle plan (ADR-0031) applies the filters and lane a correct planner would.
        plan = case.plan.as_plan(case.question) if case.plan else None
        filters = (
            merge_filters(SearchFilters(), plan, UTC, now=EVAL_NOW) if plan else SearchFilters()
        )
        recent = bool(plan and plan.wants_recent)
        # A follow-up is anchored on what the previous turn cited (P1-5), as in chat.
        anchor = (
            tuple(doc_ids[p] for p in case.history[-1].cited_documents if p in doc_ids)
            if case.history
            else ()
        )
        started = time.perf_counter()
        res = await hybrid_retrieve(
            index, ws, owner, case.question, filters, vector=vector, limit=k, recent=recent,
            anchor=anchor,
        )  # fmt: skip
        latency = (time.perf_counter() - started) * 1000
        if vector is not None and Lane.VECTOR not in res.lanes:
            raise DegradedRun(f"{case.id}: vector lane didn't run ({'; '.join(res.notices)})")
        ranked = unique_in_order([path_of.get(h.document_id, str(h.document_id)) for h in res.hits])
        expected = set(case.expected_documents)

        # The guest gets the same anchor: the lane must re-check access itself.
        as_guest = await hybrid_retrieve(
            index, ws, guest, case.question, filters, vector=vector, limit=k, recent=recent,
            anchor=anchor,
        )  # fmt: skip
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
                best_similarity=(
                    round(res.best_similarity, 4) if res.best_similarity is not None else None
                ),
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
        "answer_gate": gate_report(results, DEFAULT_MIN_SIMILARITY),
    }


def gate_report(results: Sequence[CaseResult], threshold: float) -> dict[str, object]:
    """How the answer gate (abstain below ``threshold`` best similarity) would do.

    A good threshold sits above every no-answer case's best match and below every
    answerable one's. ``suggested`` is the midpoint of that gap when one exists. The
    model's NO_ANSWER sentinel still catches what the gate lets through, so the gate
    should err low: a false abstention loses a real answer, a miss only costs a call.
    """
    answerable = [
        r.best_similarity for r in results if r.answerable and r.best_similarity is not None
    ]
    no_answer = [
        r.best_similarity for r in results if not r.answerable and r.best_similarity is not None
    ]
    if not answerable:
        return {"threshold": threshold, "note": "no vector lane: run with --embedder openai"}
    low, high = min(answerable), max(no_answer, default=None)
    return {
        "threshold": threshold,
        "answerable_min": round(low, 4),
        "no_answer_max": round(high, 4) if high is not None else None,
        "suggested": round((low + high) / 2, 4) if high is not None and high < low else None,
        "false_abstentions": [
            r.id for r in results if r.answerable and (r.best_similarity or 1) < threshold
        ],
        "no_answer_gated": [
            r.id for r in results if not r.answerable and (r.best_similarity or 1) < threshold
        ],
    }
