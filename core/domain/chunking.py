"""Chunking (ARCHITECTURE §7): pure, deterministic, property-tested.

* Never crosses a section boundary, so each chunk keeps one heading path.
* Packs whole paragraphs up to ``target`` tokens.
* Only a paragraph longer than ``target`` is split inside, by words, with ``overlap``
  tokens repeated between neighbours so a sentence cut in two stays findable.
* The embedding text gets a "title > heading" prefix; the stored content stays clean.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass

from core.domain.documents import NewChunk, Section

TokenCounter = Callable[[str], int]
TARGET_TOKENS = 512
OVERLAP_TOKENS = 64
_PARAGRAPH = re.compile(r"\n\s*\n")


@dataclass(frozen=True, slots=True)
class ChunkingConfig:
    target: int = TARGET_TOKENS
    overlap: int = OVERLAP_TOKENS

    def __post_init__(self) -> None:
        if not 0 <= self.overlap < self.target:
            raise ValueError("need 0 <= overlap < target")


def chunk_sections(
    sections: Sequence[Section], count: TokenCounter, config: ChunkingConfig = ChunkingConfig()
) -> list[NewChunk]:
    chunks: list[NewChunk] = []
    for section in sections:
        for text in _section_chunks(section.text, count, config):
            chunks.append(
                NewChunk(
                    ordinal=len(chunks),
                    content=text,
                    token_count=count(text),
                    heading_path=section.heading_path,
                )
            )
    return chunks


def embedding_text(title: str | None, chunk: NewChunk) -> str:
    """What gets embedded: context prefix + content (stored content stays clean)."""
    prefix = " > ".join(p for p in (title, chunk.heading_path) if p)
    return f"{prefix}\n\n{chunk.content}" if prefix else chunk.content


def _section_chunks(text: str, count: TokenCounter, config: ChunkingConfig) -> Iterator[str]:
    paragraphs = [p.strip() for p in _PARAGRAPH.split(text) if p.strip()]
    buffer: list[str] = []
    buffered = 0
    for para in paragraphs:
        size = count(para)
        if size > config.target:
            if buffer:
                yield "\n\n".join(buffer)
                buffer, buffered = [], 0
            yield from _split_long(para, count, config)
            continue
        joined = buffered + size + (1 if buffer else 0)
        if buffer and joined > config.target:
            yield "\n\n".join(buffer)
            buffer, buffered = [], 0
        buffer.append(para)
        buffered += size + (1 if len(buffer) > 1 else 0)
    if buffer:
        yield "\n\n".join(buffer)


def _hard_split(word: str, count: TokenCounter, target: int) -> list[str]:
    """Split a single "word" longer than the target (e.g. base64 with no spaces) by
    characters, so no chunk can exceed the embedder's input limit (security review).
    """
    size = count(word)
    if size <= target:
        return [word]
    step = max(1, len(word) * target // (size * 2))  # conservative: aim for half a window
    return [word[i : i + step] for i in range(0, len(word), step)]


def _split_long(para: str, count: TokenCounter, config: ChunkingConfig) -> Iterator[str]:
    """Greedy word packing with token overlap between consecutive windows."""
    words = [piece for word in para.split() for piece in _hard_split(word, count, config.target)]
    sizes = [count(w + " ") for w in words]
    start = 0
    while start < len(words):
        end, used = start, 0
        while end < len(words) and (used + sizes[end] <= config.target or end == start):
            used += sizes[end]
            end += 1
        yield " ".join(words[start:end])
        if end >= len(words):
            return
        # Step back far enough to repeat ~overlap tokens, but always make progress.
        back, kept = end, 0
        while back > start + 1 and kept + sizes[back - 1] <= config.overlap:
            back -= 1
            kept += sizes[back]
        start = back
