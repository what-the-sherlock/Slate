"""Source and ingestion-run bookkeeping."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from core.domain.data_class import DataClass
from core.domain.ids import DocumentId, RunId, SourceId, WorkspaceId


@dataclass(frozen=True, slots=True)
class ExistingUpload:
    document_id: DocumentId
    acl: tuple[str, ...]
    data_class: DataClass


@dataclass(frozen=True, slots=True)
class SourceRecord:
    id: SourceId
    workspace_id: WorkspaceId
    connector: str
    name: str
    visibility: tuple[str, ...]
    data_class: str
    status: str
    config: dict[str, object] = field(default_factory=dict[str, object])
    checkpoint: dict[str, object] | None = None
    last_synced_at: datetime | None = None


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

    async def finish_run(
        self,
        run_id: RunId,
        *,
        status: str,
        seen: int,
        changed: int,
        failed: int,
        error_summary: str | None = None,
    ) -> None: ...

    async def record_error(
        self, run_id: RunId, *, external_id: str | None, stage: str, message: str, retryable: bool
    ) -> None: ...

    async def upload_usage(self, ws: WorkspaceId) -> tuple[int, int]:
        """(live uploaded documents, their total bytes) in this workspace."""
        ...

    async def create_source(
        self,
        ws: WorkspaceId,
        *,
        connector: str,
        name: str,
        config: dict[str, object],
        visibility: Sequence[str],
        data_class: DataClass,
    ) -> SourceId:
        """Raises Conflict if the workspace already has a source with this name."""
        ...

    async def get_source(self, ws: WorkspaceId, source_id: SourceId) -> SourceRecord | None:
        """The source if it's in this workspace (no probing across workspaces)."""
        ...

    async def sources_by_connector(self, ws: WorkspaceId, connector: str) -> list[SourceRecord]: ...

    async def save_checkpoint(
        self, source_id: SourceId, checkpoint: dict[str, object], *, synced: bool
    ) -> None:
        """Store the resume point; ``synced`` also stamps last_synced_at (a full pass)."""
        ...

    async def due_sources(self, connector: str, limit: int) -> list[tuple[WorkspaceId, SourceId]]:
        """Active sources whose schedule_interval has passed since their last sync."""
        ...

    async def document_checksums(self, source_id: SourceId, prefix: str) -> dict[str, str]:
        """external_id -> checksum for this source's documents whose id starts with prefix."""
        ...

    async def delete_documents(self, source_id: SourceId, external_ids: Sequence[str]) -> int:
        """Hard-delete these documents (chunks and embeddings cascade)."""
        ...

    async def delete_source(self, ws: WorkspaceId, source_id: SourceId) -> bool:
        """Delete a source and everything indexed from it. False if not in this workspace."""
        ...

    async def blob_in_use(self, blob_key: str) -> bool:
        """True if a document or an unfinished job still references this object."""
        ...
