"""Model usage accounting and the monthly budget (ARCHITECTURE §12)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

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


class GenerationStore(Protocol):
    async def ensure_present(self, model: str, dims: int) -> GenerationId:
        """The 'present' generation for this model, creating it (and its HNSW index) if
        no generation is present yet. Raises Conflict if another model is present."""
        ...
