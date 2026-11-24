"""Source and ingestion-run bookkeeping."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from core.domain.data_class import DataClass
from core.domain.ids import DocumentId, RunId, SourceId, WorkspaceId


@dataclass(frozen=True, slots=True)
class ExistingUpload:
    document_id: DocumentId
    acl: tuple[str, ...]
    data_class: DataClass


class SourceStore(Protocol):
    async def upload_source(self, ws: WorkspaceId, data_class: DataClass) -> SourceId:
        """The workspace's system source for uploads of this data class (created on first use).

        Two exist because a document must share its source's data class (ADR-0012).
        """
        ...

    async def find_upload(self, ws: WorkspaceId, external_id: str) -> ExistingUpload | None:
        """An already-indexed upload with this identity, in either upload source."""
        ...

    async def create_run(self, source_id: SourceId, kind: str) -> RunId:
        """Record an ingestion run (the worker does this when it starts processing)."""
        ...
