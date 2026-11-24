"""Model ports: embedders and chat models."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Protocol

from core.domain.chat import ChatDelta, ChatMessage, Usage
from core.domain.documents import Vector


@dataclass(frozen=True, slots=True)
class Embeddings:
    vectors: list[Vector]
    tokens: int


class Tokenizer(Protocol):
    def count(self, text: str) -> int: ...


class Embedder(Protocol):
    @property
    def model(self) -> str: ...

    @property
    def dims(self) -> int: ...

    async def embed_documents(self, texts: Sequence[str]) -> Embeddings:
        """One vector per input, in order. Raises UpstreamUnavailable/RateLimited."""
        ...

    async def embed_query(self, text: str) -> Vector: ...


class ChatModel(Protocol):
    @property
    def model(self) -> str: ...

    def stream(
        self, messages: Sequence[ChatMessage], *, max_tokens: int
    ) -> AsyncIterator[ChatDelta | Usage]:
        """Yield text deltas, then exactly one Usage as the final item."""
        ...
