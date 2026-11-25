"""Model usage accounting and the monthly budget (ARCHITECTURE §12)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from core.domain.budget import Spend
from core.domain.ids import GenerationId, WorkspaceId


@dataclass(frozen=True, slots=True)
class UsageEntry:
    workspace_id: WorkspaceId
    provider: str
    model: str
    kind: str  # embed | chat | rewrite | judge
    data_class: str  # most sensitive class in the input (ADR-0012 audit)
    tokens_in: int
    tokens_out: int
    cost_usd: Decimal


class UsageLedger(Protocol):
    async def record(self, entry: UsageEntry) -> None: ...

    async def month_to_date_usd(self) -> Decimal: ...

    async def spend(self, ws: WorkspaceId) -> Spend:
        """This month's and today's spend, globally and for ``ws``, in one round trip."""
        ...


@dataclass(frozen=True, slots=True)
class GenerationInfo:
    id: GenerationId
    model: str
    dims: int
    status: str = "present"
    data_scope: str = "all"  # 'public': only public chunks may be embedded (ADR-0012)


class GenerationStore(Protocol):
    async def present(self) -> GenerationInfo | None:
        """The generation search serves from, or None before anything was embedded."""
        ...

    async def find(self, model: str) -> GenerationInfo | None:
        """The present or future generation built by this model, if any."""
        ...

    async def ensure_future(self, model: str, dims: int, *, data_scope: str) -> GenerationInfo:
        """This model's present/future generation, or a NEW 'future' one with its HNSW
        index. Raises Conflict if a different model already holds 'future'."""
        ...

    async def ensure_present(self, model: str, dims: int) -> GenerationId:
        """The 'present' generation for this model, creating it (and its HNSW index) if
        no generation is present yet. Raises Conflict if another model is present."""
        ...
