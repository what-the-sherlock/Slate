from __future__ import annotations

from uuid import uuid4

import pytest

from core.domain.documents import DocumentUpsert, NewChunk, Vector
from core.domain.errors import UpstreamUnavailable
from core.domain.ids import DocumentId, GenerationId, SourceId, WorkspaceId
from core.domain.principals import PUBLIC, workspace_principal
from core.services.search import VectorLane
from evals.cases import Case, check_references, load_cases, load_corpus
from evals.harness import CachingEmbedder, DegradedRun, run_cases
from evals.metrics import mrr_at_k, ndcg_at_k, percentile, recall_at_k, unique_in_order
from tests.fakes.models import FakeEmbedder
from tests.fakes.storage import InMemoryIndex

# ---------------------------------------------------------------- metrics


def test_metrics_on_a_known_ranking() -> None:
    ranked = ["a", "x", "b", "y"]
    assert recall_at_k(ranked, {"a", "b"}, 10) == 1.0
    assert recall_at_k(ranked, {"a", "b"}, 2) == 0.5
    assert mrr_at_k(ranked, {"b"}, 10) == pytest.approx(1 / 3)
    assert mrr_at_k(ranked, {"z"}, 10) == 0.0
    assert ndcg_at_k(["a", "b"], {"a", "b"}, 10) == pytest.approx(1.0)
    assert 0 < ndcg_at_k(["x", "a"], {"a"}, 10) < 1


def test_metric_edge_cases() -> None:
    assert unique_in_order(["a", "a", "b", "a"]) == ["a", "b"]
    assert percentile([], 50) == 0.0
    assert percentile([5.0, 1.0, 3.0], 50) == 3.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 95) == 4.0
    with pytest.raises(ValueError, match="undefined"):
        recall_at_k(["a"], set(), 10)


# ---------------------------------------------------------------- dataset


def test_real_question_set_and_corpus_are_valid() -> None:
    cases, corpus = load_cases(), load_corpus()
    check_references(cases, corpus)
    assert len(cases) >= 50
    assert {d.visibility for d in corpus} == {"public", "private"}
    # Private cases must point at private documents, or the visibility tag lies.
    private_docs = {d.path for d in corpus if d.visibility == "private"}
    for c in cases:
        if c.visibility == "private":
            assert set(c.expected_documents) <= private_docs, c.id
    assert {"keyword", "multi-hop", "no-answer", "private"} <= {t for c in cases for t in c.tags}


def test_unknown_references_and_bad_cases_are_rejected() -> None:
    with pytest.raises(ValueError, match="unknown corpus files"):
        check_references([Case(id="x", question="q", expected_documents=("nope.md",))], [])
    with pytest.raises(ValueError, match="pattern"):
        Case(id="Bad Id", question="q")


# ---------------------------------------------------------------- harness

WS = WorkspaceId(uuid4())
GEN = GenerationId(1)


async def seeded() -> tuple[InMemoryIndex, dict[str, DocumentId], set[DocumentId], FakeEmbedder]:
    index, emb, source = InMemoryIndex(), FakeEmbedder(), SourceId(uuid4())
    ids: dict[str, DocumentId] = {}
    docs = [
        ("leave.md", "Everyone gets 25 days of paid leave.", (PUBLIC,)),
        ("salary.md", "Salary bands are 96,000 to 120,000 euros.", (workspace_principal(WS),)),
    ]
    for path, body, acl in docs:
        vec = (await emb.embed_documents([body])).vectors
        doc_id, _ = await index.write_document(
            WS,
            DocumentUpsert(source, f"evals:{path}", path, path, "text/markdown", path, acl),
            [NewChunk(0, body, 6)],
            {GEN: vec},
            data_class="private",
        )
        ids[path] = doc_id
    return index, ids, {ids["salary.md"]}, emb


async def test_harness_scores_cases_and_reports_no_leaks() -> None:
    index, ids, private, emb = await seeded()
    cases = [
        Case(id="a", question="paid leave days", expected_documents=("leave.md",), tags=("x",)),
        Case(
            id="b", question="salary bands", expected_documents=("salary.md",), visibility="private"
        ),
        Case(id="c", question="dental insurance", tags=("no-answer",)),
    ]
    cached = CachingEmbedder(emb)
    report = await run_cases(index, WS, cases, ids, private, vector=VectorLane(cached, GEN))
    by_id = {c.id: c for c in report.cases}
    assert by_id["a"].recall == 1.0
    assert by_id["b"].recall == 1.0
    assert by_id["c"].recall is None
    assert report.summary["isolation"] == {"guest_leaks": 0, "cases_with_leaks": []}
    assert report.summary["cases_no_answer"] == 1
    assert cached.calls == 3  # once per case: the guest run reuses the vector


async def test_harness_detects_a_leak() -> None:
    index, ids, _, _ = await seeded()
    # Pretend the public handbook were private: the guest sees it, so it must count.
    report = await run_cases(
        index,
        WS,
        [Case(id="a", question="paid leave", expected_documents=("leave.md",))],
        ids,
        {ids["leave.md"]},
        vector=None,
    )
    assert report.summary["isolation"] == {"guest_leaks": 1, "cases_with_leaks": ["a"]}


async def test_harness_refuses_silent_keyword_only_numbers() -> None:
    index, ids, private, _ = await seeded()

    class Down(FakeEmbedder):
        async def embed_query(self, text: str) -> Vector:
            raise UpstreamUnavailable("down")

    with pytest.raises(DegradedRun):
        await run_cases(
            index,
            WS,
            [Case(id="a", question="leave")],
            ids,
            private,
            vector=VectorLane(Down(), GEN),
        )
