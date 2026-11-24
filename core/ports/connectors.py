"""Connector protocol family (ARCHITECTURE §6).

A connector declares its capabilities by which protocols it implements. Connectors
only fetch: they never parse, chunk, embed or write to the database (rule 2).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import datetime
from typing import ClassVar, Protocol

from core.domain.connectors import Checkpoint, CheckResult, RateLimit, SourceDocument


class Connector(Protocol):
    kind: ClassVar[str]

    def validate(self) -> CheckResult: ...

    def rate_limit(self) -> RateLimit: ...


class FullLoad(Connector, Protocol):
    def load(self, checkpoint: Checkpoint | None) -> Iterator[SourceDocument | Checkpoint]: ...


class Poll(Connector, Protocol):
    def poll(
        self, since: datetime, checkpoint: Checkpoint | None
    ) -> Iterator[SourceDocument | Checkpoint]: ...


class SlimList(Connector, Protocol):
    """List current external ids, used to prune deleted documents."""

    def list_ids(self) -> Iterator[str]: ...


class WebhookReceiver(Connector, Protocol):
    def verify(self, headers: Mapping[str, str], body: bytes) -> bool: ...

    def changed_ids(self, body: bytes) -> list[str]: ...
