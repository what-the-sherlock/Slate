"""Read models for the Sources page. Every query is scoped by workspace AND principals."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from core.domain.ids import JobId, RunId, SourceId, UserId, WorkspaceId
from core.domain.principals import Principals
from core.domain.source_status import SourceFacts


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
    created_by: UserId | None = None  # who connected it (ADR-0026); None for upload sources
    # Raw facts for the status badge (core.domain.source_status, P0-2).
    run_started_at: datetime | None = None
    run_skipped: Mapping[str, int] = field(default_factory=dict[str, int])
    jobs_running: int = 0
    running_since: datetime | None = None
    jobs_waiting: int = 0
    oldest_waiting_due: datetime | None = None
    newest_waiting_due: datetime | None = None
    retry_error: str | None = None
    dead_error: str | None = None
    dead_at: datetime | None = None
    last_ok_run_at: datetime | None = None

    def facts(self) -> SourceFacts:
        return SourceFacts(
            status=self.status, documents=self.documents, last_synced_at=self.last_synced_at,
            run_status=self.last_run_status, run_error=self.last_run_error,
            run_started_at=self.run_started_at, run_skipped=self.run_skipped,
            jobs_running=self.jobs_running, running_since=self.running_since,
            jobs_waiting=self.jobs_waiting, oldest_waiting_due=self.oldest_waiting_due,
            newest_waiting_due=self.newest_waiting_due,
            retry_error=self.retry_error, dead_error=self.dead_error, dead_at=self.dead_at,
            last_ok_run_at=self.last_ok_run_at,
        )  # fmt: skip


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
    skipped: Mapping[str, int] = field(default_factory=dict[str, int])


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

    async def recent_runs(
        self, ws: WorkspaceId, source_ids: list[SourceId], limit: int
    ) -> list[tuple[SourceId, RunSummary]]:
        """The newest runs of these sources (already checked visible), newest first; no errors."""
        ...

    async def runs(
        self, ws: WorkspaceId, principals: Principals, source_id: SourceId, limit: int
    ) -> list[RunSummary]:
        """Raises NotFound if the source isn't visible to these principals."""
        ...

    async def job(self, ws: WorkspaceId, job_id: JobId) -> JobView:
        """Raises NotFound."""
        ...


@dataclass(frozen=True, slots=True)
class ActivityRun:
    """One run in the workspace-wide history, with its source."""

    run: RunSummary
    source_id: SourceId
    source_name: str
    connector: str
