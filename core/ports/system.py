"""System ports: time, ids, health, parsing."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from core.domain.documents import ParsedDocument


class Clock(Protocol):
    def now(self) -> datetime:
        """Timezone-aware UTC now."""
        ...


class IdGenerator(Protocol):
    def new_uuid(self) -> UUID: ...


class HealthProbe(Protocol):
    async def database_ok(self) -> bool:
        """True if a trivial query succeeds within a short timeout."""
        ...

    async def migration_revision(self) -> str | None:
        """The DB's current Alembic revision, or None if never migrated."""
        ...


class Parser(Protocol):
    """Sync on purpose: parsing is CPU-bound. The worker calls it directly; the connected
    engine, which also runs inline in a request, calls it through asyncio.to_thread."""

    def supports(self, mime: str) -> bool: ...

    def parse(self, data: bytes, mime: str) -> ParsedDocument:
        """Raises ValidationFailed for unreadable or unsupported input."""
        ...
