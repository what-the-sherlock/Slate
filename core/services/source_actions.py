"""Actions on a connected source, whatever its connector: sync now and disconnect.

``POST /sources/{id}/sync`` and ``DELETE /sources/{id}`` don't know which connector a source
belongs to. This looks the source up *in the caller's workspace* (so another workspace's
id is a plain 404) and hands it to that connector's service, which does its own role and
ownership checks. Upload sources have neither action, so they 404 here too.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from core.domain.auth import Identity
from core.domain.errors import NotFound, UpstreamUnavailable
from core.domain.ids import JobId, SourceId
from core.ports.sources import SourceStore
from core.services.inline import InlineOutcome, InlineRunner

NOT_FOUND = "This source doesn't exist."


class ConnectorActions(Protocol):
    @property
    def available(self) -> bool: ...

    async def sync_now(self, identity: Identity, source_id: SourceId) -> JobId: ...

    async def disconnect(self, identity: Identity, source_id: SourceId) -> None: ...


@dataclass(frozen=True, slots=True)
class ConnectorEntry:
    name: str  # for messages: "GitHub", "Discord"
    actions: ConnectorActions
    inline: InlineRunner | None = None  # runs a manual sync right away (~90 s), else worker


class SourceActions:
    def __init__(self, sources: SourceStore, connectors: Mapping[str, ConnectorEntry]) -> None:
        self._sources = sources
        self._connectors = dict(connectors)

    async def _entry(self, identity: Identity, source_id: SourceId) -> ConnectorEntry:
        source = await self._sources.get_source(identity.workspace_id, source_id)
        # Same rule as the sources list (catalog SQL): a source you can't see doesn't exist
        # for you, so you can't sync it, delete it, or learn that it's there (ADR-0026).
        visible = source is not None and bool(
            set(source.visibility) & set(identity.principals.as_list())
        )
        entry = self._connectors.get(source.connector) if source is not None and visible else None
        if entry is None:
            raise NotFound(NOT_FOUND)
        return entry

    async def sync_now(
        self, identity: Identity, source_id: SourceId
    ) -> tuple[JobId, InlineOutcome]:
        entry = await self._entry(identity, source_id)
        if not entry.actions.available:
            raise UpstreamUnavailable(f"{entry.name} isn't set up on this server yet.")
        job = await entry.actions.sync_now(identity, source_id)
        outcome = InlineOutcome.DEFERRED
        if entry.inline is not None:
            # Up to ~90 s here; a big source continues in the background from its checkpoint.
            outcome = await entry.inline.run(job)
        return job, outcome

    async def disconnect(self, identity: Identity, source_id: SourceId) -> None:
        # Disconnecting works even when the connector is no longer configured: deleting
        # your data must never depend on the integration still being set up.
        entry = await self._entry(identity, source_id)
        await entry.actions.disconnect(identity, source_id)
