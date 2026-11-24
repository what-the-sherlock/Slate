"""Readiness: can this instance serve traffic?"""

from __future__ import annotations

from dataclasses import dataclass

from core.ports.system import HealthProbe


@dataclass(frozen=True, slots=True)
class Readiness:
    ready: bool
    database: bool
    migration_revision: str | None
    expected_revision: str | None

    @property
    def migrations_current(self) -> bool:
        return self.migration_revision == self.expected_revision


async def check_readiness(probe: HealthProbe, expected_revision: str | None) -> Readiness:
    """Ready when the DB answers and its schema is at the code's migration head.

    A revision mismatch means a deploy shipped code before (or without) running its
    migrations, so the instance reports not-ready instead of failing on real requests.
    """
    db_ok = await probe.database_ok()
    revision = await probe.migration_revision() if db_ok else None
    current = db_ok and revision == expected_revision
    return Readiness(
        ready=current,
        database=db_ok,
        migration_revision=revision,
        expected_revision=expected_revision,
    )
