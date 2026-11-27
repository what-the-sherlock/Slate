"""Why a source item wasn't indexed (P0-4). Pure: no I/O.

A sync counts every item it saw but didn't index, by reason, so "0 documents" always
comes with an explanation. Counts are stored per run as a flat map (``ingestion_runs.
skipped``) whose keys are ``reason`` or ``reason:kind``, e.g. ``unsupported_type:pdf``.
Keys are bounded (``MAX_KEYS``) so a run can never store an unbounded JSON blob.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from enum import StrEnum


class SkipReason(StrEnum):
    UNSUPPORTED_TYPE = "unsupported_type"  # a file type Slate doesn't read yet
    TOO_LARGE = "too_large"  # over a per-file size cap
    EMPTY_TEXT = "empty_text"  # readable, but no text in it
    UNREADABLE = "unreadable"  # the provider wouldn't hand over its content
    EXCLUDED_PATH = "excluded_path"  # vendored, generated or hidden directories
    CAP_REACHED = "cap_reached"  # a per-run limit stopped the pass (count = items left out,
    # or 1 when the provider can't say how many)


MAX_KEYS = 24
_REASONS = frozenset(r.value for r in SkipReason)
_KIND = re.compile(r"[^a-z0-9_-]+")

# Human labels for the ``kind`` part (plural forms); unknown kinds show as "files".
_KIND_LABELS: dict[str, tuple[str, str]] = {
    "pdf": ("PDF", "PDFs"),
    "sheet": ("Sheet", "Sheets"),
    "slides": ("Slides deck", "Slides decks"),
    "word": ("Word file", "Word files"),
    "image": ("image", "images"),
    "video": ("video", "videos"),
    "code": ("code or other file", "code and other files"),
    "file": ("file", "files"),
    "message": ("message", "messages"),
    "thread": ("conversation", "conversations"),
}
_REASON_TEXT: dict[str, str] = {
    SkipReason.UNSUPPORTED_TYPE: "not supported yet",
    SkipReason.TOO_LARGE: "too large",
    SkipReason.EMPTY_TEXT: "no text",
    SkipReason.UNREADABLE: "couldn't be read",
    SkipReason.EXCLUDED_PATH: "in vendored or hidden folders",
    SkipReason.CAP_REACHED: "over the per-sync limit",
}


def skip_key(reason: SkipReason, kind: str | None = None) -> str:
    if not kind:
        return reason.value
    clean = _KIND.sub("", kind.lower())[:24]
    return f"{reason.value}:{clean}" if clean else reason.value


class Skips:
    """A mutable per-run tally. Bounded: once ``MAX_KEYS`` keys exist, new kinds fold into
    their bare reason so nothing is lost from the total."""

    def __init__(self, initial: Mapping[str, int] | None = None) -> None:
        self._counts: dict[str, int] = {}
        for k, n in (initial or {}).items():
            self._add_key(k, n)

    def add(self, reason: SkipReason, kind: str | None = None, n: int = 1) -> None:
        if n > 0:
            self._add_key(skip_key(reason, kind), n)

    def merge(self, other: Mapping[str, object]) -> None:
        """Untrusted input (stored JSON, adapters): unknown reasons and non-counts dropped."""
        for k, n in other.items():
            if _valid_key(k) and isinstance(n, int) and not isinstance(n, bool) and n > 0:
                self._add_key(k, n)

    def _add_key(self, key: str, n: int) -> None:
        if key not in self._counts and len(self._counts) >= MAX_KEYS:
            key = key.split(":", 1)[0]
        self._counts[key] = self._counts.get(key, 0) + n

    @property
    def total(self) -> int:
        return sum(self._counts.values())

    def as_json(self) -> dict[str, int]:
        return dict(sorted(self._counts.items()))


def _valid_key(key: str) -> bool:
    return key.split(":", 1)[0] in _REASONS


def describe_skips(skipped: Mapping[str, int], *, limit: int = 3) -> str | None:
    """A short, plain sentence for the UI: "30 PDFs (not supported yet), 7 Sheets (not
    supported yet)". None when nothing was skipped."""
    items = sorted(
        ((k, n) for k, n in skipped.items() if n > 0 and _valid_key(k)), key=lambda kv: -kv[1]
    )
    if not items:
        return None
    parts = [_phrase(k, n) for k, n in items[:limit]]
    rest = sum(n for _, n in items[limit:])
    if rest:
        parts.append(f"{rest} more")
    return ", ".join(parts)


def _phrase(key: str, n: int) -> str:
    reason, _, kind = key.partition(":")
    if reason == SkipReason.CAP_REACHED and n == 1 and not kind:
        return "stopped at the per-sync limit"
    singular, plural = _KIND_LABELS.get(kind or "file", (kind, f"{kind} files"))
    return f"{n} {singular if n == 1 else plural} ({_REASON_TEXT.get(reason, reason)})"
