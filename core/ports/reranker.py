"""A selection step between retrieval and generation (IMPROVEMENT_PLAN P1-4, ADR-0040).

Given the question and short candidate summaries, it returns which candidates are worth
reading, best first. Optional and off by default (``CHAT_RERANK``). Any failure falls back to
the fused rank order.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from core.domain.chat import Usage


@dataclass(frozen=True, slots=True)
class RerankCandidate:
    n: int  # its position in the pool, 1-based: what the reranker answers with
    title: str
    kind: str | None
    date: str | None
    snippet: str  # the first characters of the passage only


@dataclass(frozen=True, slots=True)
class Selection:
    keep: tuple[int, ...]  # candidate numbers, best first; () means "could not decide"
    usage: Usage | None = None


class Reranker(Protocol):
    @property
    def model(self) -> str: ...

    async def select(
        self, question: str, candidates: Sequence[RerankCandidate], keep: int
    ) -> Selection:
        """At most ``keep`` candidate numbers, best first. Raises UpstreamUnavailable or
        RateLimited when the provider fails; the caller falls back to rank order."""
        ...
