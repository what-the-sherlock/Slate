"""Read models for the Sources page. Every query is scoped by workspace AND principals."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from core.domain.ids import JobId, RunId, SourceId, UserId, WorkspaceId
from core.domain.principals import Principals


@dataclass(frozen=True, slots=True)
class SourceSummary:
    id: SourceId
    name: str
    connector: str
    data_class: str
    visibility: tuple[str, ...]
    status: str
    documents: int  # only documents the viewer may see
    last_synced_at: datetime | None
    last_run_status: str | None
    last_run_error: str | None
    pending_jobs: int


@dataclass(frozen=True, slots=True)
class RunError:
    stage: str
    message: str
    retryable: bool
    external_id: str | None


@dataclass(frozen=True, slots=True)
class RunSummary:
    id: RunId
    kind: str
    status: str
    started_at: datetime | None
    finished_at: datetime | None
    docs_seen: int
    docs_changed: int
    docs_failed: int
    error_summary: str | None
    errors: tuple[RunError, ...]


@dataclass(frozen=True, slots=True)
class JobView:
    id: JobId
    status: str
    attempts: int
    last_error: str | None
    filename: str | None
    uploader: UserId | None


class Catalog(Protocol):
    async def sources(self, ws: WorkspaceId, principals: Principals) -> list[SourceSummary]: ...

    async def runs(
        self, ws: WorkspaceId, principals: Principals, source_id: SourceId, limit: int
    ) -> list[RunSummary]:
        """Raises NotFound if the source isn't visible to these principals."""
        ...

    async def job(self, ws: WorkspaceId, job_id: JobId) -> JobView:
        """Raises NotFound."""
        ...
