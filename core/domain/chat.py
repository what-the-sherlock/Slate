"""Chat model I/O types."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


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
