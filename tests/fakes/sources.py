from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from core.domain.data_class import DataClass
from core.domain.errors import Conflict
from core.domain.ids import DocumentId, RunId, SourceId, UserId, WorkspaceId
from core.ports.sources import ExistingUpload, LastRun, SourceRecord
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
        self.skipped: dict[RunId, dict[str, int]] = {}
        self.overdue = False  # worker_overdue()
        self.progressed: list[tuple[RunId, int, int, int]] = []
        self.used_blobs: set[str] = set()
        self.usage: dict[WorkspaceId, tuple[int, int]] = {}  # (documents, bytes)

    async def upload_source(self, ws: WorkspaceId, data_class: DataClass) -> SourceId:
        key = (ws, data_class)
        if key not in self.sources:
            sid = SourceId(self._ids.new_uuid())
            self.sources[key] = sid
            name = "Uploads" if data_class is DataClass.PRIVATE else "Public uploads"
            self.records[sid] = SourceRecord(
                sid, ws, "upload", name, (f"workspace:{ws}",), data_class.value, "active"
            )
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
        # Mirrors ingestion_runs' kind CHECK (0011): a made-up kind fails in Postgres.
        assert kind in {"full", "incremental", "prune", "upload", "webhook", "import"}, kind
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
        skipped: Mapping[str, int] | None = None,
    ) -> None:
        # Mirrors ingestion_runs' CHECKs (migrations 0001, 0011).
        assert status in {"succeeded", "partial", "failed", "cancelled"}, status
        self.finished[run_id] = (status, seen, changed, failed, error_summary)
        self.skipped[run_id] = dict(skipped or {})

    async def last_run(self, source_id: SourceId) -> LastRun | None:
        mine = [r for r, (sid, _) in self.runs.items() if sid == source_id and r in self.finished]
        if not mine:
            return None
        status, *_, summary = self.finished[mine[-1]]
        return LastRun(status, summary, self.skipped.get(mine[-1], {}))

    async def worker_overdue(self, ws: WorkspaceId) -> bool:
        return self.overdue

    async def progress(self, run_id: RunId, *, seen: int, changed: int, failed: int) -> None:
        self.progressed.append((run_id, seen, changed, failed))

    async def record_error(
        self, run_id: RunId, *, external_id: str | None, stage: str, message: str, retryable: bool
    ) -> None:
        # Mirrors ingestion_errors' CHECK (migration 0001): a made-up stage fails in Postgres.
        assert stage in {"fetch", "parse", "chunk", "embed", "store"}, f"invalid stage {stage!r}"
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
        created_by: UserId | None = None,
        credential_id: UUID | None = None,
        connection_id: UUID | None = None,
    ) -> SourceId:
        if any(r.workspace_id == ws and r.name == name for r in self.records.values()):
            raise Conflict(f"{name} is already connected.")
        sid = SourceId(self._ids.new_uuid())
        self.records[sid] = SourceRecord(
            sid,
            ws,
            connector,
            name,
            tuple(visibility),
            data_class.value,
            "active",
            dict(config),
            created_by=created_by,
            credential_id=credential_id,
            connection_id=connection_id,
        )
        return sid

    async def set_credential(
        self, ws: WorkspaceId, source_id: SourceId, credential_id: UUID
    ) -> bool:
        r = self.records.get(source_id)
        if r is None or r.workspace_id != ws:
            return False
        self.records[source_id] = replace(r, credential_id=credential_id)
        return True

    async def update_config(
        self, ws: WorkspaceId, source_id: SourceId, config: dict[str, object]
    ) -> bool:
        r = self.records.get(source_id)
        if r is None or r.workspace_id != ws:
            return False
        self.records[source_id] = replace(r, config=dict(config))
        return True

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

    async def document_versions(
        self, source_id: SourceId, prefix: str
    ) -> dict[str, tuple[str, datetime | None]]:
        if self._index is None:
            return {}
        return self._index.versions(source_id, prefix)

    async def config_in_use(self, connector: str, key: str, value: str) -> bool:
        return any(
            r.connector == connector and str(r.config.get(key)) == value
            for r in self.records.values()
        )

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
