"""What a source's status badge says, from raw facts (P0-2). Pure: no I/O.

The rule this enforces: the badge never looks healthy when something is wrong, and every
unhealthy state says why and what to do. Precedence, first match wins:

1. ``paused``.
2. A job is running: ``syncing`` (unless it has been running far longer than any job may,
   which means its worker died: then ``waiting_for_worker``).
3. A job is waiting (and not superseded: a run that worked started after every waiting job
   was due, so they're leftovers) and is *overdue*: runnable for over ``WORKER_OVERDUE``.
   The worker drains every runnable job each run, so an overdue job proves no worker
   handles that kind: the sync workflow isn't running, or runs without that handler's
   secrets.
4. A job is waiting after a failure: ``retrying`` with the job's error.
5. A job is queued: ``queued``.
6. The newest job is ``dead`` and newer than the last run that worked: ``failed`` with its
   error (this also repairs history written before P0-1, when crashes looked like successes).
7. The last run is still ``running`` with no job behind it: it was killed (e.g. a serverless
   time limit): ``failed`` ("interrupted").
8. The last run's own status: ``failed`` / ``partial``.
9. Synced but nothing indexed: ``empty``, with what was skipped and why.
10. ``synced``, or ``never``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from core.domain.skips import describe_skips

WORKER_OVERDUE = timedelta(hours=2)
RUN_STUCK = timedelta(minutes=15)  # a running run with no job: the process died
JOB_STUCK = timedelta(minutes=30)  # past the worker's stale-heartbeat reclaim

WAITING_FOR_WORKER = (
    "Waiting for the background worker, which hasn't picked this up in over 2 hours. "
    "Check that the sync workflow is running."
)
INTERRUPTED = "The last sync was interrupted before it finished. Try syncing again."
EMPTY = "Nothing to index was found."


class Badge(StrEnum):
    PAUSED = "paused"
    SYNCING = "syncing"
    QUEUED = "queued"
    RETRYING = "retrying"
    WAITING_FOR_WORKER = "waiting_for_worker"
    FAILED = "failed"
    PARTIAL = "partial"
    EMPTY = "empty"
    SYNCED = "synced"
    NEVER = "never"


@dataclass(frozen=True, slots=True)
class SourceFacts:
    status: str  # sources.status
    documents: int
    last_synced_at: datetime | None
    run_status: str | None = None  # the newest ingestion run
    run_error: str | None = None
    run_started_at: datetime | None = None
    run_skipped: Mapping[str, int] = field(default_factory=dict[str, int])
    jobs_running: int = 0
    running_since: datetime | None = None  # the oldest running job's heartbeat or lock
    jobs_waiting: int = 0  # queued, or failed and due for a retry
    oldest_waiting_due: datetime | None = None  # min(run_after) of the waiting ones
    newest_waiting_due: datetime | None = None  # max(run_after) of the waiting ones
    retry_error: str | None = None  # last_error of the newest waiting failed job
    dead_error: str | None = None  # last_error when the newest job is dead
    dead_at: datetime | None = None
    last_ok_run_at: datetime | None = None  # newest succeeded/partial run start


@dataclass(frozen=True, slots=True)
class Status:
    badge: Badge
    reason: str | None = None

    @property
    def busy(self) -> bool:
        """Counted in "Processing N sources": work that is actually moving."""
        return self.badge in {Badge.SYNCING, Badge.QUEUED, Badge.RETRYING}

    @property
    def healthy(self) -> bool:
        return self.badge in {Badge.SYNCED, Badge.SYNCING, Badge.QUEUED, Badge.PAUSED}


def source_status(f: SourceFacts, now: datetime) -> Status:
    if f.status == "paused":
        return Status(Badge.PAUSED)
    if f.jobs_running:
        if f.running_since is not None and now - f.running_since > JOB_STUCK:
            return Status(Badge.WAITING_FOR_WORKER, WAITING_FOR_WORKER)
        return Status(Badge.SYNCING)
    if f.jobs_waiting and not _superseded(f):
        overdue = f.oldest_waiting_due is not None and now - f.oldest_waiting_due > WORKER_OVERDUE
        if overdue:
            return Status(Badge.WAITING_FOR_WORKER, _join(WAITING_FOR_WORKER, f.retry_error))
        if f.retry_error:
            return Status(Badge.RETRYING, f"Retrying: {_plain(f.retry_error)}")
        return Status(Badge.QUEUED)
    if f.dead_error and (
        f.last_ok_run_at is None or (f.dead_at is not None and f.dead_at > f.last_ok_run_at)
    ):
        return Status(Badge.FAILED, f"Stopped after repeated failures: {_plain(f.dead_error)}")
    if f.run_status == "running":
        stuck = f.run_started_at is None or now - f.run_started_at > RUN_STUCK
        return Status(Badge.FAILED, INTERRUPTED) if stuck else Status(Badge.SYNCING)
    if f.run_status == "failed":
        return Status(Badge.FAILED, f.run_error)
    if f.run_status in {"partial", "cancelled"}:
        return Status(Badge.PARTIAL, f.run_error)
    if f.last_synced_at is None and f.run_status is None:
        return Status(Badge.NEVER)
    if f.documents == 0:
        skipped = describe_skips(f.run_skipped)
        return Status(Badge.EMPTY, f"Skipped {skipped}." if skipped else EMPTY)
    return Status(Badge.SYNCED)


def _superseded(f: SourceFacts) -> bool:
    """Every waiting job was due before a run that worked: leftovers (e.g. retries of a bug
    that a deploy fixed). They'll run again harmlessly (checksums); they aren't the news."""
    return (
        f.last_ok_run_at is not None
        and f.newest_waiting_due is not None
        and f.last_ok_run_at > f.newest_waiting_due
    )


def _plain(error: str) -> str:
    """Job errors are short and content-free by construction ("unexpected KeyError", a
    domain error's detail). Make the internal form readable."""
    if error.startswith("unexpected "):
        return f"Slate hit an internal error ({error.removeprefix('unexpected ')})."
    return error


def _join(first: str, error: str | None) -> str:
    return f"{first} Last error: {_plain(error)}" if error else first
