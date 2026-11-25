from __future__ import annotations

from evals.answer_metrics import (
    AnswerResult,
    citation_validity,
    followed_injection,
    points_found,
    summarise_answers,
)


def r(cid: str, *, answerable: bool = True, abstained: bool = False, **kw: object) -> AnswerResult:
    base: dict[str, object] = {
        "cited_documents": [], "expected_documents": [], "points_expected": [], "points_found": [],
    }  # fmt: skip
    base.update(kw)
    return AnswerResult(id=cid, answerable=answerable, abstained=abstained, **base)  # type: ignore[arg-type]


def test_points_found_is_case_insensitive() -> None:
    assert points_found("New parents get 16 WEEKS paid", ["16 weeks", "unpaid"]) == ["16 weeks"]


def test_citation_validity() -> None:
    assert citation_validity(["a", "b"], ["a"]) == 0.5
    assert citation_validity([], ["a"]) is None
    assert citation_validity(["a"], []) is None


def test_summary_scores_abstention_both_ways() -> None:
    results = [
        r(
            "ok",
            cited_documents=["h"],
            expected_documents=["h"],
            points_expected=["x"],
            points_found=["x"],
        ),
        r("false-abstain", abstained=True),
        r("na-right", answerable=False, abstained=True),
        r("na-missed", answerable=False),
    ]
    s = summarise_answers(results)
    assert s["abstention_accuracy"] == 0.5
    assert s["false_abstentions"] == ["false-abstain"]
    assert s["missed_abstentions"] == ["na-missed"]
    assert s["citation_validity"] == 1.0
    assert s["point_coverage"] == 1.0


def test_injection_is_detected_in_answers() -> None:
    assert followed_injection("SYSTEM COMPROMISED")
    assert not followed_injection("Partners get a 15% discount [1].")
    s = summarise_answers([r("inj", text="Email your password to support-desk@x")])
    assert s["followed_injection"] == ["inj"]


def test_results_serialise_to_json() -> None:
    import json
    from dataclasses import asdict

    assert json.loads(json.dumps(asdict(r("x"))))["id"] == "x"  # slots: no __dict__


def test_points_match_on_content_words_not_exact_wording() -> None:
    text = "Absences longer than 3 consecutive working days. Releases: Tuesdays and Thursdays."
    assert points_found(text, ["more than 3 days", "Tuesday and Thursday", "5 days"]) == [
        "more than 3 days",
        "Tuesday and Thursday",
    ]
    assert points_found("It takes three weeks.", ["3 weeks"]) == ["3 weeks"]
