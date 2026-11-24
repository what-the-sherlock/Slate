from __future__ import annotations

import hashlib
import math
import struct
from collections.abc import AsyncIterator, Sequence

from core.domain.chat import ChatDelta, ChatMessage, Usage
from core.domain.documents import Vector
from core.ports.models import Embeddings


class FakeEmbedder:
    """Deterministic unit vectors derived from a hash of the text. No network, no keys.

    Identical texts get identical vectors, so similarity tests are stable.
    """

    def __init__(self, dims: int = 1024, model: str = "fake-embed") -> None:
        self._dims = dims
        self._model = model
        self.calls: list[int] = []  # batch sizes, to assert batching behaviour

    @property
    def model(self) -> str:
        return self._model

    @property
    def dims(self) -> int:
        return self._dims

    def _vector(self, text: str) -> list[float]:
        out: list[float] = []
        counter = 0
        while len(out) < self._dims:
            digest = hashlib.sha256(f"{counter}:{text}".encode()).digest()
            out.extend(v / 2**31 for v in struct.unpack(">8i", digest))
            counter += 1
        vec = out[: self._dims]
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]

    async def embed_documents(self, texts: Sequence[str]) -> Embeddings:
        self.calls.append(len(texts))
        return Embeddings(
            vectors=[self._vector(t) for t in texts],
            tokens=sum(len(t.split()) for t in texts),
        )

    async def embed_query(self, text: str) -> Vector:
        return self._vector(text)


class WordTokenizer:
    """Deterministic, offline token counting for tests: one token per word."""

    def count(self, text: str) -> int:
        return len(text.split())


class FakeChatModel:
    """Streams a scripted reply word by word, then reports usage."""

    def __init__(self, reply: str = "Fake answer [1].", model: str = "fake-chat") -> None:
        self.reply = reply
        self._model = model
        self.received: list[Sequence[ChatMessage]] = []

    @property
    def model(self) -> str:
        return self._model

    async def stream(
        self, messages: Sequence[ChatMessage], *, max_tokens: int
    ) -> AsyncIterator[ChatDelta | Usage]:
        self.received.append(messages)
        words = self.reply.split(" ")
        for i, word in enumerate(words[:max_tokens]):
            yield ChatDelta(word if i == 0 else " " + word)
        tokens_in = sum(len(m.content.split()) for m in messages)
        yield Usage(model=self._model, tokens_in=tokens_in, tokens_out=len(words))
