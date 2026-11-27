"""Connected sources (ADR-0033): what an OAuth provider's connector hands the sync engine.

A connector only *fetches* (CLAUDE.md rule 2). Each call returns one ``Page``: documents
already shaped as sections (by this provider's pure rules), the ids of documents that are
gone, and an opaque checkpoint to resume from. ``done`` says whether this pass is complete.
The engine (``core/services/connected.py``) does the rest: checksum skip, indexing, deleting,
saving the checkpoint after every page, budget, time and reconnect handling.

Pure: no I/O.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from core.domain.documents import Section

# One source pass reads at most this many documents; the rest follow on the next run (the
# checkpoint keeps the place), so no run is unbounded.
MAX_DOCUMENTS_PER_RUN = 2_000
MAX_SECTION_CHARS = 100_000
MAX_TITLE_CHARS = 300

# Provider ids (``connections.provider``) whose data Slate indexes, and the connector name
# their sources use (``sources.connector``): the same string, by design.
CONNECTED_PROVIDERS = ("gmail", "notion", "linear", "google_calendar", "google_drive", "slack")


@dataclass(frozen=True, slots=True)
class FetchedDocument:
    """One provider item ready to index. ``external_id`` is stable across syncs."""

    external_id: str
    title: str
    uri: str
    sections: tuple[Section, ...]
    mime: str = "text/plain"
    author: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    metadata: Mapping[str, object] = field(default_factory=dict[str, object])
    # A file the engine must parse (a PDF, a CSV export; P1-1), of type ``mime``. The
    # connector only fetches (CLAUDE.md rule 2): with ``raw`` set, ``sections`` is empty and
    # the engine fills it with the same parser uploads use.
    raw: bytes | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class Page:
    documents: tuple[FetchedDocument, ...] = ()
    deleted: tuple[str, ...] = ()  # external ids that no longer exist at the provider
    checkpoint: Mapping[str, object] = field(default_factory=dict[str, object])
    done: bool = True  # this pass has nothing more to read
    # On the last page of a *full* pass: every external id that still exists. The engine
    # deletes the source's other documents (providers without a change feed of deletions).
    keep_only: tuple[str, ...] | None = None
    # Delete every document whose external id starts with one of these (e.g. a Slack
    # channel the user left: all of its day documents).
    deleted_prefixes: tuple[str, ...] = ()
    # Items on this page that weren't turned into documents, and why:
    # {"reason[:kind]": count} (core.domain.skips; P0-4).
    skipped: Mapping[str, int] = field(default_factory=dict[str, int])


def document_checksum(doc: FetchedDocument) -> str:
    """Changes whenever anything indexed changes (title, sections, author, dates)."""
    h = hashlib.sha256()
    for part in (doc.title, doc.author or "", _iso(doc.updated_at), _iso(doc.created_at)):
        h.update(part.encode() + b"\x00")
    for s in doc.sections:
        h.update((s.heading_path or "").encode() + b"\x00" + s.text.encode() + b"\x01")
    return h.hexdigest()


def cap_sections(sections: Sequence[Section]) -> tuple[Section, ...]:
    """Keep documents bounded: at most MAX_SECTION_CHARS of text, empty sections dropped."""
    out: list[Section] = []
    used = 0
    for s in sections:
        text = s.text.strip()
        if not text:
            continue
        room = MAX_SECTION_CHARS - used
        if room <= 0:
            break
        if len(text) > room:
            out.append(Section(s.heading_path, text[:room] + "\n(truncated)"))
            break
        out.append(Section(s.heading_path, text))
        used += len(text)
    return tuple(out)


def clean_title(title: str, fallback: str) -> str:
    one_line = " ".join(title.split())
    return (one_line or fallback)[:MAX_TITLE_CHARS]


def checkpoint_json(checkpoint: Mapping[str, object]) -> dict[str, object]:
    """A checkpoint as stored: JSON round-tripped, so a connector can't smuggle in objects."""
    loaded: object = json.loads(json.dumps(checkpoint, default=str))
    return loaded if isinstance(loaded, dict) else {}  # type: ignore[return-value]


def _iso(when: datetime | None) -> str:
    return when.isoformat() if when else ""
