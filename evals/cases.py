"""Eval inputs: the question set and the synthetic corpus, validated on load.

Both are files in git, so they're reviewed like code. Pydantic rejects a malformed case
up front instead of producing a silently wrong metric.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from core.domain.attachments import AttachmentInfo, AttachmentState
from core.domain.planner import QueryPlan

ROOT = Path(__file__).resolve().parent
QUESTIONS = ROOT / "questions.yaml"
CORPUS = ROOT / "corpus"
EXTERNAL_PREFIX = "evals:"
# The eval world's "now" (ADR-0031): questions like "last week" are about the dated
# connector documents, so answer evals pin the clock here and stay stable over time.
EVAL_NOW = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)
EVAL_TIMEZONE = "UTC"
CorpusKind = Literal["file", "email", "github", "discord"]
# The connector each corpus kind is loaded as, so source-kind filters work in evals.
CONNECTOR_BY_KIND: dict[CorpusKind, str] = {
    "file": "upload",
    "email": "imap",
    "github": "github",
    "discord": "discord",
}


class OraclePlan(BaseModel):
    """The plan a perfect planner would make for a case. The free retrieval eval applies
    it, so the recent lane and filters are measured without paying for the planner; the
    paid answer eval ignores it and measures the real planner."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    intent: Literal["lookup", "recent", "summary"] = "lookup"
    after: date | None = None
    before: date | None = None
    sources: tuple[Literal["email", "github", "discord", "file"], ...] = ()
    sender: str | None = None
    # Vague questions (ADR-0043).
    direction: Literal["sent", "received"] | None = None
    person: str | None = None
    since_hours: int | None = Field(default=None, ge=1, le=72)
    wants_content: bool = False

    def as_plan(self, question: str) -> QueryPlan:
        return QueryPlan(
            queries=(question,),
            intent=self.intent,
            after=self.after,
            before=self.before,
            kinds=self.sources,
            sender=self.sender,
            planned=True,
            direction=self.direction,
            person=self.person,
            since_hours=self.since_hours,
            wants_content=self.wants_content,
        )


class Turn(BaseModel):
    """An earlier turn of a multi-turn case (IMPROVEMENT_PLAN P1-8): the question asked and
    the documents its answer cited. The follow-up is scored; earlier turns set context."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    question: str = Field(min_length=1, max_length=500)
    cited_documents: tuple[str, ...] = ()


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z0-9-]+$")
    question: str = Field(min_length=1, max_length=500)
    expected_documents: tuple[str, ...] = ()
    expected_answer_points: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    visibility: Literal["public", "private"] = "public"
    # A question no source answers but Slate should still answer (ADR-0025): "general"
    # (general knowledge, labelled) or "chat" (small talk). None: no-answer cases abstain.
    expect_mode: Literal["general", "chat"] | None = None
    plan: OraclePlan | None = None
    # Earlier turns, oldest first. Empty: a one-shot question.
    history: tuple[Turn, ...] = ()
    # Cross-source cases: the answer must cite documents from at least this many corpus
    # kinds (file, email, github, discord). Scored by the answer eval only.
    min_cited_kinds: int | None = Field(default=None, ge=2, le=4)

    @property
    def answerable(self) -> bool:
        return bool(self.expected_documents)


def load_cases(path: Path = QUESTIONS) -> list[Case]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    cases = TypeAdapter(list[Case]).validate_python(raw)
    ids = [c.id for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate case ids in questions.yaml")
    return cases


@dataclass(frozen=True, slots=True)
class CorpusDoc:
    path: str  # relative, e.g. "handbook/leave-policy.md"
    title: str
    visibility: Literal["public", "private"]
    body: bytes  # Markdown without the front matter
    checksum: str
    # Connector-shaped documents (ADR-0031): what kind of source they imitate, who wrote
    # them and when, so dates, senders and source kinds can be evaluated.
    kind: CorpusKind = "file"
    author: str | None = None
    updated_at: datetime | None = None
    # Email (ADR-0043): the folder it sits in and everyone in it; an attachment's text
    # names its parent document (loaded as a child, like production stores it).
    folder: Literal["sent", "inbox"] | None = None
    people: tuple[str, ...] = ()
    parent: str | None = None

    @property
    def external_id(self) -> str:
        return EXTERNAL_PREFIX + self.path


def load_corpus(root: Path = CORPUS) -> list[CorpusDoc]:
    docs: list[CorpusDoc] = []
    for file in sorted(root.rglob("*.md")):
        rel = file.relative_to(root).as_posix()
        meta, body = _front_matter(file.read_text(encoding="utf-8"), rel)
        data = body.encode()
        kind = meta.get("kind", "file")
        if kind not in CONNECTOR_BY_KIND:
            raise ValueError(f"{rel}: unknown kind {kind!r}")
        visibility = "private" if meta.get("visibility") == "private" else "public"
        if kind != "file" and visibility != "private":
            raise ValueError(f"{rel}: connector documents are always private (ADR-0026)")
        docs.append(
            CorpusDoc(
                path=rel,
                title=meta["title"],
                visibility=visibility,
                body=data,
                checksum=_checksum(data, meta),
                kind=kind,
                author=meta.get("author"),
                updated_at=_when(meta.get("date"), rel),
                folder=_folder(meta.get("folder"), rel),
                people=tuple(p.strip() for p in meta.get("people", "").split(",") if p.strip()),
                parent=meta.get("parent"),
            )
        )
    paths = {d.path for d in docs}
    for d in docs:
        if d.parent is not None and d.parent not in paths:
            raise ValueError(f"{d.path}: parent {d.parent} is not in the corpus")
    # Parents before their attachments (the database needs the parent first).
    return sorted(docs, key=lambda d: (d.parent is not None, d.path))


def document_metadata(doc: CorpusDoc, corpus: Sequence[CorpusDoc]) -> dict[str, object]:
    """What a connector would store for this document (ADR-0043): folders, people, and
    Tier 0 of the attachments whose text is loaded as its children."""
    meta: dict[str, object] = {"corpus_path": doc.path}
    if doc.folder:
        meta["folders"] = [doc.folder]
    if doc.people:
        meta["people"] = list(doc.people)
        meta["recipients"] = ", ".join(doc.people[1:])
    files = [f for f in corpus if f.parent == doc.path]
    if files:
        meta["attachments"] = [
            AttachmentInfo(
                key=f"eval-{i}", name=f.title.split(" (attached to:")[0], mime="text/markdown",
                size=len(f.body), state=AttachmentState.TEXT,
            ).to_meta()
            for i, f in enumerate(files)
        ]  # fmt: skip
    return meta


def _folder(raw: str | None, rel: str) -> Literal["sent", "inbox"] | None:
    if raw is None:
        return None
    if raw not in ("sent", "inbox"):
        raise ValueError(f"{rel}: folder must be sent or inbox")
    return "sent" if raw == "sent" else "inbox"


_METADATA_KEYS = ("kind", "author", "date", "folder", "people", "parent")


def _checksum(data: bytes, meta: dict[str, str]) -> str:
    """The body's SHA-256; for documents with metadata, the metadata too, since it's
    indexed (changing a date must re-load). Plain files keep their old checksums, so the
    existing corpus isn't re-embedded."""
    h = hashlib.sha256(data)
    extra = [f"{k}={meta[k]}" for k in _METADATA_KEYS if k in meta]
    if extra:
        h.update(("\x00" + "\x00".join(extra)).encode())
    return h.hexdigest()


def _when(raw: str | None, rel: str) -> datetime | None:
    if raw is None:
        return None
    try:
        when = datetime.fromisoformat(raw)
    except ValueError as e:
        raise ValueError(f"{rel}: date must be ISO 8601") from e
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def check_references(cases: list[Case], corpus: list[CorpusDoc]) -> None:
    """Every expected document must exist, so a typo can't masquerade as a miss."""
    known = {d.path for d in corpus}
    referenced = {p for c in cases for p in c.expected_documents} | {
        p for c in cases for t in c.history for p in t.cited_documents
    }
    missing = {p for p in referenced if p not in known}
    if missing:
        raise ValueError(f"questions.yaml refers to unknown corpus files: {sorted(missing)}")


def _front_matter(text: str, rel: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        raise ValueError(f"{rel}: missing front matter")
    head, sep, body = text[4:].partition("\n---\n")
    if not sep:
        raise ValueError(f"{rel}: unterminated front matter")
    meta = yaml.safe_load(head) or {}
    if not isinstance(meta, dict) or not isinstance(meta.get("title"), str):
        raise ValueError(f"{rel}: front matter needs a title")
    return {str(k): str(v) for k, v in meta.items()}, body
