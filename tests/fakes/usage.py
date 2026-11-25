from __future__ import annotations

from decimal import Decimal

from core.domain.budget import Spend
from core.domain.errors import Conflict
from core.domain.ids import GenerationId, WorkspaceId
from core.ports.usage import GenerationInfo, UsageEntry


class InMemoryLedger:
    def __init__(self, spent: Decimal = Decimal(0)) -> None:
        self.entries: list[UsageEntry] = []
        self.spent = spent
        self.workspace_spent: dict[WorkspaceId, Decimal] = {}  # extra per-workspace spend

    async def record(self, entry: UsageEntry) -> None:
        self.entries.append(entry)
        self.spent += entry.cost_usd

    async def month_to_date_usd(self) -> Decimal:
        return self.spent

    async def spend(self, ws: WorkspaceId) -> Spend:
        """Everything counts as this month and today (the fake has no clock). Goes
        through month_to_date_usd so tests that patch it still drive the global total."""
        total = await self.month_to_date_usd()
        mine = sum((e.cost_usd for e in self.entries if e.workspace_id == ws), Decimal(0))
        mine += self.workspace_spent.get(ws, Decimal(0))
        return Spend(month_all=total, day_all=total, month_workspace=mine, day_workspace=mine)


class InMemoryGenerations:
    def __init__(self) -> None:
        self.present_gen: tuple[GenerationId, str] | None = None
        self.future_gen: GenerationInfo | None = None

    async def find(self, model: str) -> GenerationInfo | None:
        present = await self.present()
        if present is not None and present.model == model:
            return present
        if self.future_gen is not None and self.future_gen.model == model:
            return self.future_gen
        return None

    async def ensure_future(self, model: str, dims: int, *, data_scope: str) -> GenerationInfo:
        found = await self.find(model)
        if found is not None:
            return found
        if self.future_gen is not None:
            raise Conflict("another model is being built")
        self.future_gen = GenerationInfo(GenerationId(2), model, dims, "future", data_scope)
        return self.future_gen

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
