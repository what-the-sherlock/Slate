from __future__ import annotations

from core.domain.skips import MAX_KEYS, SkipReason, Skips, describe_skips, skip_key


def test_keys_are_reason_and_a_sanitised_kind() -> None:
    assert skip_key(SkipReason.UNSUPPORTED_TYPE, "PDF") == "unsupported_type:pdf"
    assert skip_key(SkipReason.TOO_LARGE, "a b/../c") == "too_large:abc"
    assert skip_key(SkipReason.EMPTY_TEXT, "") == "empty_text"
    assert skip_key(SkipReason.EMPTY_TEXT, "!!!") == "empty_text"


def test_the_tally_is_bounded_and_loses_nothing_from_the_total() -> None:
    s = Skips()
    for i in range(MAX_KEYS + 10):
        s.add(SkipReason.UNSUPPORTED_TYPE, f"kind{i}")
    assert len(s.as_json()) <= MAX_KEYS + 1  # overflow folds into the bare reason
    assert s.total == MAX_KEYS + 10


def test_merge_drops_untrusted_junk() -> None:
    s = Skips()
    s.merge(
        {
            "too_large:file": 2,
            "made_up": 5,
            "empty_text": -1,
            "cap_reached": True,
            "unreadable": "3",
        }
    )
    assert s.as_json() == {"too_large:file": 2}


def test_descriptions_are_plain_and_short() -> None:
    assert describe_skips({}) is None
    assert describe_skips({"unsupported_type:pdf": 30, "unsupported_type:sheet": 7}) == (
        "30 PDFs (not supported yet), 7 Sheets (not supported yet)"
    )
    assert describe_skips({"too_large:file": 1}) == "1 file (too large)"
    assert describe_skips({"cap_reached": 1}) == "stopped at the per-sync limit"
    many = {
        "unsupported_type:pdf": 9,
        "too_large:file": 5,
        "empty_text:file": 3,
        "excluded_path": 2,
        "unreadable:file": 1,
    }
    assert describe_skips(many) == (
        "9 PDFs (not supported yet), 5 files (too large), 3 files (no text), 3 more"
    )
