"""Chat model I/O types."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from core.domain.errors import ValidationFailed


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: Role
    content: str


@dataclass(frozen=True, slots=True)
class ChatDelta:
    text: str


@dataclass(frozen=True, slots=True)
class Usage:
    model: str
    tokens_in: int
    tokens_out: int


MAX_TITLE_CHARS = 120


def clean_title(raw: str) -> str:
    """A chat title a person typed (ADR-0044): one line, printable characters only,
    whitespace collapsed, at most ``MAX_TITLE_CHARS``. Raises ValidationFailed if nothing
    is left."""
    printable = "".join(ch if ch.isprintable() else " " for ch in raw)
    title = " ".join(printable.split())[:MAX_TITLE_CHARS].rstrip()
    if not title:
        message = "Give the chat a name."
        raise ValidationFailed(message, extra={"fields": {"title": message}})
    return title
