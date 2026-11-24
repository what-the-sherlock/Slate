from __future__ import annotations

from decimal import Decimal

from core.domain.errors import Conflict
from core.domain.ids import GenerationId
from core.ports.usage import GenerationInfo, UsageEntry


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
        self.present_gen: tuple[GenerationId, str] | None = None

    async def present(self) -> GenerationInfo | None:
        if self.present_gen is None:
            return None
        return GenerationInfo(self.present_gen[0], self.present_gen[1], 1024)

    async def ensure_present(self, model: str, dims: int) -> GenerationId:
        if self.present_gen is None:
            self.present_gen = (GenerationId(1), model)
        if self.present_gen[1] != model:
            raise Conflict("another model is present")
        return self.present_gen[0]
