from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import TYPE_CHECKING

from core.domain.data_class import DataClass
from core.domain.errors import Conflict
from core.domain.ids import DocumentId, RunId, SourceId, WorkspaceId
from core.ports.sources import ExistingUpload, SourceRecord
from tests.fakes.system import SequentialIds

if TYPE_CHECKING:
    from tests.fakes.storage import InMemoryIndex


class InMemorySources:
    def __init__(self, index: InMemoryIndex | None = None) -> None:
        self._ids = SequentialIds()
        self._index = index  # documents live there (document_checksums, delete_documents)
        self.records: dict[SourceId, SourceRecord] = {}
        self.synced: list[SourceId] = []
        self.sources: dict[tuple[WorkspaceId, DataClass], SourceId] = {}
        self.uploads: dict[tuple[WorkspaceId, str], ExistingUpload] = {}
        self.runs: dict[RunId, tuple[SourceId, str]] = {}
        self.finished: dict[RunId, tuple[str, int, int, int, str | None]] = {}
        self.errors: list[tuple[RunId, str, str, bool]] = []
        self.used_blobs: set[str] = set()
        self.usage: dict[WorkspaceId, tuple[int, int]] = {}  # (documents, bytes)

    async def upload_source(self, ws: WorkspaceId, data_class: DataClass) -> SourceId:
        key = (ws, data_class)
        if key not in self.sources:
            self.sources[key] = SourceId(self._ids.new_uuid())
        return self.sources[key]

    async def upload_usage(self, ws: WorkspaceId) -> tuple[int, int]:
        return self.usage.get(ws, (0, 0))

    async def find_upload(self, ws: WorkspaceId, external_id: str) -> ExistingUpload | None:
        return self.uploads.get((ws, external_id))

    def index(
        self, ws: WorkspaceId, external_id: str, acl: tuple[str, ...], data_class: DataClass
    ) -> DocumentId:
        """Test helper: pretend the worker indexed this upload."""
        doc = DocumentId(self._ids.new_uuid())
        self.uploads[(ws, external_id)] = ExistingUpload(doc, acl, data_class)
        return doc

    async def create_run(self, source_id: SourceId, kind: str) -> RunId:
        run = RunId(self._ids.new_uuid())
        self.runs[run] = (source_id, kind)
        return run

    async def finish_run(
        self,
        run_id: RunId,
        *,
        status: str,
        seen: int,
        changed: int,
        failed: int,
        error_summary: str | None = None,
    ) -> None:
        self.finished[run_id] = (status, seen, changed, failed, error_summary)

    async def record_error(
        self, run_id: RunId, *, external_id: str | None, stage: str, message: str, retryable: bool
    ) -> None:
        self.errors.append((run_id, stage, message, retryable))

    async def blob_in_use(self, blob_key: str) -> bool:
        return blob_key in self.used_blobs

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
        if any(r.workspace_id == ws and r.name == name for r in self.records.values()):
            raise Conflict(f"{name} is already connected.")
        sid = SourceId(self._ids.new_uuid())
        self.records[sid] = SourceRecord(
            sid, ws, connector, name, tuple(visibility), data_class.value, "active", dict(config)
        )
        return sid

    async def get_source(self, ws: WorkspaceId, source_id: SourceId) -> SourceRecord | None:
        r = self.records.get(source_id)
        return r if r is not None and r.workspace_id == ws else None

    async def sources_by_connector(self, ws: WorkspaceId, connector: str) -> list[SourceRecord]:
        return sorted(
            (r for r in self.records.values() if r.workspace_id == ws and r.connector == connector),
            key=lambda r: r.name,
        )

    async def save_checkpoint(
        self, source_id: SourceId, checkpoint: dict[str, object], *, synced: bool
    ) -> None:
        r = self.records[source_id]
        self.records[source_id] = replace(r, checkpoint=dict(checkpoint))
        if synced:
            self.synced.append(source_id)

    async def due_sources(self, connector: str, limit: int) -> list[tuple[WorkspaceId, SourceId]]:
        due = [
            (r.workspace_id, r.id)
            for r in self.records.values()
            if r.connector == connector and r.status == "active" and r.id not in self.synced
        ]
        return due[:limit]

    async def document_checksums(self, source_id: SourceId, prefix: str) -> dict[str, str]:
        if self._index is None:
            return {}
        return self._index.checksums(source_id, prefix)

    async def delete_documents(self, source_id: SourceId, external_ids: Sequence[str]) -> int:
        if self._index is None:
            return 0
        return self._index.delete(source_id, external_ids)

    async def delete_source(self, ws: WorkspaceId, source_id: SourceId) -> bool:
        r = self.records.get(source_id)
        if r is None or r.workspace_id != ws or r.connector == "upload":
            return False
        del self.records[source_id]
        if self._index is not None:
            self._index.delete(source_id, list(self._index.checksums(source_id, "")))
        return True
