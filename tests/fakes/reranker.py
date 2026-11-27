from __future__ import annotations

from collections.abc import Sequence

from core.domain.chat import Usage
from core.domain.errors import UpstreamUnavailable
from core.ports.reranker import RerankCandidate, Selection


class FakeReranker:
    """Keeps a scripted list of candidate numbers (default: the pool reversed), or fails.
    Records what it was shown, so tests can check that only summaries left Slate."""

    def __init__(self, keep: Sequence[int] | None = None, *, fail: bool = False) -> None:
        self._keep = tuple(keep) if keep is not None else None
        self._fail = fail
        self.calls: list[tuple[str, list[RerankCandidate], int]] = []

    @property
    def model(self) -> str:
        return "fake-rerank"

    async def select(
        self, question: str, candidates: Sequence[RerankCandidate], keep: int
    ) -> Selection:
        self.calls.append((question, list(candidates), keep))
        if self._fail:
            raise UpstreamUnavailable("reranker down")
        chosen = self._keep if self._keep is not None else tuple(c.n for c in reversed(candidates))
        return Selection(chosen[:keep], Usage("fake-rerank", 100, 10))
