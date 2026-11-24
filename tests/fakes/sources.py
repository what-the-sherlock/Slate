from __future__ import annotations

from core.domain.data_class import DataClass
from core.domain.ids import DocumentId, RunId, SourceId, WorkspaceId
from core.ports.sources import ExistingUpload
from tests.fakes.system import SequentialIds


class InMemorySources:
    def __init__(self) -> None:
        self._ids = SequentialIds()
        self.sources: dict[tuple[WorkspaceId, DataClass], SourceId] = {}
        self.uploads: dict[tuple[WorkspaceId, str], ExistingUpload] = {}
        self.runs: dict[RunId, tuple[SourceId, str]] = {}
        self.finished: dict[RunId, tuple[str, int, int, int, str | None]] = {}
        self.errors: list[tuple[RunId, str, str, bool]] = []
        self.used_blobs: set[str] = set()

    async def upload_source(self, ws: WorkspaceId, data_class: DataClass) -> SourceId:
        key = (ws, data_class)
        if key not in self.sources:
            self.sources[key] = SourceId(self._ids.new_uuid())
        return self.sources[key]

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
