"""System clock and id generator."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class UuidGenerator:
    def new_uuid(self) -> uuid.UUID:
        return uuid.uuid4()
