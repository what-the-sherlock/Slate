from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID


class FakeClock:
    """Controllable time: tests advance it explicitly."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


class SequentialIds:
    """Deterministic UUIDs: 00000000-0000-0000-0000-000000000001, ...02, ..."""

    def __init__(self) -> None:
        self._n = 0

    def new_uuid(self) -> UUID:
        self._n += 1
        return UUID(int=self._n)


class FakeHealthProbe:
    def __init__(self, *, db_ok: bool = True, revision: str | None = "head") -> None:
        self.db_ok = db_ok
        self.revision = revision

    async def database_ok(self) -> bool:
        return self.db_ok

    async def migration_revision(self) -> str | None:
        return self.revision


class FakeWorkerTrigger:
    def __init__(self) -> None:
        self.reasons: list[str] = []

    async def trigger(self, reason: str) -> None:
        self.reasons.append(reason)
