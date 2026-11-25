from __future__ import annotations

from evals.harness import CaseResult, gate_report


def case(cid: str, sim: float | None, *, answerable: bool = True) -> CaseResult:
    return CaseResult(
        id=cid, tags=(), visibility="public", answerable=answerable, top_documents=[],
        recall=None, mrr=None, ndcg=None, latency_ms=1.0, best_similarity=sim,
    )  # fmt: skip


def test_suggests_the_midpoint_when_the_cases_separate() -> None:
    report = gate_report([case("a", 0.62), case("b", 0.48), case("n", 0.21, answerable=False)], 0.3)
    assert report["answerable_min"] == 0.48
    assert report["no_answer_max"] == 0.21
    assert report["suggested"] == 0.345
    assert report["false_abstentions"] == []
    assert report["no_answer_gated"] == ["n"]


def test_no_suggestion_when_the_cases_overlap() -> None:
    report = gate_report([case("a", 0.3), case("n", 0.4, answerable=False)], 0.35)
    assert report["suggested"] is None
    assert report["false_abstentions"] == ["a"]


def test_keyword_only_runs_say_so() -> None:
    assert "note" in gate_report([case("a", None)], 0.3)
