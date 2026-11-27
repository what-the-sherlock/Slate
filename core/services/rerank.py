"""The ``Reranker`` port over any ``ChatModel`` (P1-4, ADR-0040).

It needs no vendor SDK of its own: the same chat adapter (gpt-4o-mini) answers one small
JSON question. Wired only when ``CHAT_RERANK`` is on.
"""

from __future__ import annotations

from collections.abc import Sequence

from core.domain.chat import ChatDelta
from core.domain.rerank import RERANK_MAX_TOKENS, parse_selection, rerank_messages
from core.ports.models import ChatModel
from core.ports.reranker import RerankCandidate, Selection


class LLMReranker:
    def __init__(self, model: ChatModel) -> None:
        self._model = model

    @property
    def model(self) -> str:
        return self._model.model

    async def select(
        self, question: str, candidates: Sequence[RerankCandidate], keep: int
    ) -> Selection:
        messages = rerank_messages(
            question, [(c.n, c.title, c.kind, c.date, c.snippet) for c in candidates], keep
        )
        text, usage = "", None
        async for item in self._model.stream(messages, max_tokens=RERANK_MAX_TOKENS):
            if isinstance(item, ChatDelta):
                text += item.text
            else:
                usage = item
        return Selection(parse_selection(text, {c.n for c in candidates}, keep), usage)
