"""The hidden FOLLOWUPS line (P2-1): never streamed, parsed defensively."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from core.domain.answer import MAX_FOLLOWUP_CHARS, FollowupFilter, parse_followups

ANSWER = "Leave is 25 days [1].\nCarryover is 5 days [2].\n"
LINE = 'FOLLOWUPS: ["Who approves it?", "What about part-timers?"]'


def run(chunks: list[str]) -> tuple[str, tuple[str, ...]]:
    f = FollowupFilter()
    shown = "".join(f.feed(c) for c in chunks) + f.flush()
    return shown, f.followups


def test_the_line_is_stripped_and_parsed() -> None:
    shown, followups = run([ANSWER + LINE])
    assert shown == ANSWER
    assert followups == ("Who approves it?", "What about part-timers?")


@given(st.lists(st.integers(min_value=1, max_value=12), min_size=1, max_size=40))
def test_any_chunking_gives_the_same_result(sizes: list[int]) -> None:
    text = ANSWER + LINE
    chunks: list[str] = []
    i = 0
    for n in sizes:
        chunks.append(text[i : i + n])
        i += n
    chunks.append(text[i:])
    assert run(chunks) == (ANSWER, ("Who approves it?", "What about part-timers?"))


def test_text_that_only_looks_like_the_marker_still_streams() -> None:
    shown, followups = run(["FOLL", "OW the money, said Ines [1]."])
    assert shown == "FOLLOW the money, said Ines [1]."
    assert followups == ()
    shown, _ = run(["The word FOLLOWUPS: inside a line is not the marker."])
    assert shown == "The word FOLLOWUPS: inside a line is not the marker."


def test_an_answer_without_the_line_is_unchanged() -> None:
    assert run([ANSWER]) == (ANSWER, ())
    assert run(["Short"]) == ("Short", ())  # a held prefix is released at the end


def test_suggestions_are_validated() -> None:
    long = "x" * (MAX_FOLLOWUP_CHARS + 1)
    raw = f'FOLLOWUPS: ["A?", "a?", "Cites [1]?", 7, "", "{long}", "B?", "C?", "D?"]'
    assert parse_followups(raw) == ("A?", "B?", "C?")
    for bad in ("FOLLOWUPS: none", "FOLLOWUPS: [broken", 'FOLLOWUPS: {"a": 1}'):
        assert parse_followups(bad) == ()
