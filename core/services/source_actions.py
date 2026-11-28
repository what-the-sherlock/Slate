"""Actions on a connected source, whatever its connector: sync now and disconnect.

``POST /sources/{id}/sync`` and ``DELETE /sources/{id}`` don't know which connector a source
belongs to. This looks the source up *in the caller's workspace* (so another workspace's
id is a plain 404) and hands it to that connector's service, which does its own role and
ownership checks. Upload sources have neither action, so they 404 here too.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from enum import StrEnum
from typing import Protocol

import structlog

from core.domain.auth import Identity
from core.domain.errors import AppError, NotFound, UpstreamUnavailable
from core.domain.ids import JobId, SourceId
from core.domain.skips import describe_skips
from core.domain.source_status import WAITING_FOR_WORKER
from core.ports.jobs import WorkerTrigger
from core.ports.sources import SourceStore
from core.services.inline import InlineOutcome, InlineRunner

log = structlog.get_logger(__name__)

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
    quick: InlineRunner | None = None  # a short sync a question can wait for (ADR-0043)


# Freshness (ADR-0043): a question about the last hours checks its sources first.
FRESH_FOR = timedelta(minutes=10)  # synced this recently: nothing to do
QUICK_COOLDOWN = timedelta(minutes=2)  # at most one question-driven sync per source
MAX_QUICK_SOURCES = 2  # per question
QUICK_TIMEOUT_S = 25.0  # hard stop; the sync's own budget is shorter (cooperative)


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

    async def freshen(
        self, identity: Identity, connectors: Sequence[str], *, now: datetime, zone: tzinfo
    ) -> tuple[str, ...]:
        """Before answering a question about the last hours: sync this user's sources of
        these connectors that haven't synced in ``FRESH_FOR`` (a short, cooperative sync,
        at most ``MAX_QUICK_SOURCES``, once per source per ``QUICK_COOLDOWN``). Returns a
        note for each source still not fresh, so the answer can say what it couldn't see.
        Never raises: a failed refresh only means the note is shown."""
        notes: list[str] = []
        tried = 0
        mine = set(identity.principals.as_list())
        for connector in dict.fromkeys(connectors):
            entry = self._connectors.get(connector)
            if entry is None:
                continue
            for source in await self._sources.sources_by_connector(
                identity.workspace_id, connector
            ):
                if source.status != "active" or not set(source.visibility) & mine:
                    continue
                synced = source.last_synced_at
                if synced is not None and now - synced < FRESH_FOR:
                    continue
                if tried < MAX_QUICK_SOURCES and await self._quick(identity, entry, source.id, now):
                    tried += 1
                    fresh = await self._sources.get_source(identity.workspace_id, source.id)
                    synced = fresh.last_synced_at if fresh is not None else synced
                    if synced is not None and now - synced < FRESH_FOR:
                        continue
                notes.append(stale_note(entry.name, synced, now, zone))
        return tuple(notes)

    async def _quick(
        self, identity: Identity, entry: ConnectorEntry, source_id: SourceId, now: datetime
    ) -> bool:
        """Run one quick sync; True when one ran (whatever it finished)."""
        if entry.quick is None or not entry.actions.available:
            return False
        run = await self._sources.last_run(source_id)
        if run is not None and run.started_at is not None and now - run.started_at < QUICK_COOLDOWN:
            return False  # one is running or just ran: don't pile up syncs
        try:
            job = await entry.actions.sync_now(identity, source_id)
            outcome = await entry.quick.run(job, timeout_s=QUICK_TIMEOUT_S)
        except AppError as e:  # not allowed, rate limited, provider down: just report
            log.info("freshen.skipped", code=e.code)
            return False
        if outcome is not InlineOutcome.DONE and self._trigger is not None:
            await self._trigger.trigger("freshen")  # the worker finishes it
        return True

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


def stale_note(name: str, synced: datetime | None, now: datetime, zone: tzinfo) -> str:
    """'Gmail last synced at 13:17 (48 minutes ago), so newer items may not be in Slate
    yet.' Times in the user's zone."""
    if synced is None:
        return f"{name} hasn't finished its first sync, so its newest items may be missing."
    minutes = max(int((now - synced).total_seconds() // 60), 0)
    if minutes < 90:
        ago = f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    elif minutes < 48 * 60:
        ago = f"{minutes // 60} hours ago"
    else:
        ago = f"{minutes // (24 * 60)} days ago"
    local = synced.astimezone(zone)
    when = f"{local:%H:%M}" if (now - synced) < timedelta(hours=20) else f"{local:%d %b %H:%M}"
    return f"{name} last synced at {when} ({ago}), so newer items may not be in Slate yet."
