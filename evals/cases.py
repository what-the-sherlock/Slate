"""Eval inputs: the question set and the synthetic corpus, validated on load.

Both are files in git, so they're reviewed like code. Pydantic rejects a malformed case
up front instead of producing a silently wrong metric.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

ROOT = Path(__file__).resolve().parent
QUESTIONS = ROOT / "questions.yaml"
CORPUS = ROOT / "corpus"
EXTERNAL_PREFIX = "evals:"


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

    @property
    def external_id(self) -> str:
        return EXTERNAL_PREFIX + self.path


def load_corpus(root: Path = CORPUS) -> list[CorpusDoc]:
    docs: list[CorpusDoc] = []
    for file in sorted(root.rglob("*.md")):
        rel = file.relative_to(root).as_posix()
        meta, body = _front_matter(file.read_text(encoding="utf-8"), rel)
        data = body.encode()
        docs.append(
            CorpusDoc(
                path=rel,
                title=meta["title"],
                visibility="private" if meta.get("visibility") == "private" else "public",
                body=data,
                checksum=hashlib.sha256(data).hexdigest(),
            )
        )
    return docs


def check_references(cases: list[Case], corpus: list[CorpusDoc]) -> None:
    """Every expected document must exist, so a typo can't masquerade as a miss."""
    known = {d.path for d in corpus}
    missing = {p for c in cases for p in c.expected_documents if p not in known}
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
