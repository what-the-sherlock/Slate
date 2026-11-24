from __future__ import annotations

from decimal import Decimal

from core.domain.errors import Conflict
from core.domain.ids import GenerationId
from core.ports.usage import UsageEntry


class InMemoryLedger:
    def __init__(self, spent: Decimal = Decimal(0)) -> None:
        self.entries: list[UsageEntry] = []
        self.spent = spent

    async def record(self, entry: UsageEntry) -> None:
        self.entries.append(entry)
        self.spent += entry.cost_usd

    async def month_to_date_usd(self) -> Decimal:
        return self.spent


class InMemoryGenerations:
    def __init__(self) -> None:
        self.present: tuple[GenerationId, str] | None = None

    async def ensure_present(self, model: str, dims: int) -> GenerationId:
        if self.present is None:
            self.present = (GenerationId(1), model)
        if self.present[1] != model:
            raise Conflict("another model is present")
        return self.present[0]
