"""Actions on a connected source, whatever its connector: sync now and disconnect.

``POST /sources/{id}/sync`` and ``DELETE /sources/{id}`` don't know which connector a source
belongs to. This looks the source up *in the caller's workspace* (so another workspace's
id is a plain 404) and hands it to that connector's service, which does its own role and
ownership checks. Upload sources have neither action, so they 404 here too.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from core.domain.auth import Identity
from core.domain.errors import NotFound, UpstreamUnavailable
from core.domain.ids import JobId, SourceId
from core.domain.skips import describe_skips
from core.domain.source_status import WAITING_FOR_WORKER
from core.ports.jobs import WorkerTrigger
from core.ports.sources import SourceStore
from core.services.inline import InlineOutcome, InlineRunner

NOT_FOUND = "This source doesn't exist."


class ConnectorActions(Protocol):
    @property
    def available(self) -> bool: ...

    async def sync_now(self, identity: Identity, source_id: SourceId) -> JobId: ...

    async def disconnect(self, identity: Identity, source_id: SourceId) -> None: ...


class SyncStatus(StrEnum):
    DONE = "done"  # synced now, fully
    PARTIAL = "partial"  # synced part; ``reason`` says what's left and why
    FAILED = "failed"  # it didn't work; ``reason`` says why
    BACKGROUND = "background"  # the worker does it (no inline runner, busy, or too big)


@dataclass(frozen=True, slots=True)
class SyncResult:
    job_id: JobId
    status: SyncStatus
    reason: str | None = None
    skipped: str | None = None  # what the run left out and why (P0-4), e.g. "3 PDFs (...)"


CRASHED = "Slate hit an internal error while syncing. It will retry automatically."
LATER = "The rest continues when the background worker runs."
CLOCK_SKEW = timedelta(seconds=30)  # API clock vs database clock


@dataclass(frozen=True, slots=True)
class ConnectorEntry:
    name: str  # for messages: "GitHub", "Discord"
    actions: ConnectorActions
    inline: InlineRunner | None = None  # runs a manual sync right away (~90 s), else worker


class SourceActions:
    def __init__(
        self,
        sources: SourceStore,
        connectors: Mapping[str, ConnectorEntry],
        trigger: WorkerTrigger | None = None,
    ) -> None:
        self._sources = sources
        self._connectors = dict(connectors)
        self._trigger = trigger

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

    async def sync_now(self, identity: Identity, source_id: SourceId) -> SyncResult:
        result = await self._sync_now(identity, source_id)
        if result.status is not SyncStatus.DONE and self._trigger is not None:
            # Work was handed to the worker (the rest of a big sync, a retry): start it now
            # instead of waiting hours for GitHub's cron (ADR-0035). Best effort.
            await self._trigger.trigger(f"sync_now:{result.status.value}")
        return result

    async def _sync_now(self, identity: Identity, source_id: SourceId) -> SyncResult:
        """Queue a sync and, where an inline runner exists, run it now (up to ~90 s).

        The answer comes from the *run* the sync recorded, not from the job: a job can
        finish while its run failed (a repo that's gone, a revoked grant), and a crashed job
        is retried by the worker but did not work now. Neither is "continuing" (P0-2).
        """
        entry = await self._entry(identity, source_id)
        if not entry.actions.available:
            raise UpstreamUnavailable(f"{entry.name} isn't set up on this server yet.")
        job = await entry.actions.sync_now(identity, source_id)
        if entry.inline is None:
            return await self._background(source_id, job)
        started = datetime.now(UTC) - CLOCK_SKEW
        # Up to ~90 s here; a big source continues in the background from its checkpoint.
        outcome = await entry.inline.run(job)
        run = await self._sources.last_run(source_id)
        if run is not None and run.started_at is not None and run.started_at < started:
            run = None  # an earlier sync's run: not what happened just now
        if outcome is InlineOutcome.FAILED:
            reason = run.error_summary if run is not None and run.status == "failed" else None
            return SyncResult(job, SyncStatus.FAILED, reason or CRASHED)
        if outcome is InlineOutcome.DEFERRED:
            if run is not None and run.status in {"partial", "cancelled", "running"}:
                # It did part of the work here (e.g. stopped at the time limit); the worker
                # does the rest. That's progress, not a failure.
                done = f"Synced {run.seen} item(s) so far." if run.seen else None
                reason = " ".join(x for x in (done, run.error_summary, LATER) if x)
                return SyncResult(job, SyncStatus.PARTIAL, reason, describe_skips(run.skipped))
            return await self._background(source_id, job)
        if run is None:  # the job finished without a run: the source was gone or paused
            return SyncResult(job, SyncStatus.DONE)
        if run.status == "failed":
            return SyncResult(job, SyncStatus.FAILED, run.error_summary)
        skipped = describe_skips(run.skipped)
        if run.status in {"partial", "cancelled", "running"}:
            return SyncResult(job, SyncStatus.PARTIAL, run.error_summary, skipped)
        return SyncResult(job, SyncStatus.DONE, None, skipped)

    async def _background(self, source_id: SourceId, job: JobId) -> SyncResult:
        if await self._sources.worker_overdue(source_id):
            # Earlier work is still waiting: saying "it continues" would be a promise.
            return SyncResult(job, SyncStatus.FAILED, WAITING_FOR_WORKER)
        return SyncResult(job, SyncStatus.BACKGROUND)

    async def disconnect(self, identity: Identity, source_id: SourceId) -> None:
        # Disconnecting works even when the connector is no longer configured: deleting
        # your data must never depend on the integration still being set up.
        entry = await self._entry(identity, source_id)
        await entry.actions.disconnect(identity, source_id)
