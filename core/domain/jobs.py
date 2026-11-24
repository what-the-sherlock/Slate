"""Background job model and retry policy (pure; shared by every JobQueue adapter)."""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from core.domain.ids import JobId, WorkspaceId

JsonObject = dict[str, object]


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"  # failed this attempt, will retry at run_after
    DEAD = "dead"  # out of attempts or not retryable; shown in the admin UI


@dataclass(frozen=True, slots=True)
class Job:
    id: JobId
    workspace_id: WorkspaceId
    kind: str
    payload: JsonObject
    status: JobStatus
    attempts: int
    max_attempts: int
    run_after: datetime
    idempotency_key: str
    locked_by: str | None = None
    heartbeat_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime | None = field(default=None, compare=False)


BASE_BACKOFF = timedelta(seconds=30)
MIN_BACKOFF = timedelta(seconds=5)
MAX_BACKOFF = timedelta(hours=1)
STALE_AFTER = timedelta(minutes=10)


def retry_delay(attempt: int, rng: random.Random | None = None) -> timedelta:
    """Exponential backoff with jitter: uniform(MIN, min(cap, base * 2**(attempt-1))).

    Jitter spreads retries out so many failed jobs don't all hammer an upstream service
    at the same instant (the "thundering herd"). The small floor stops a first retry
    from landing immediately, i.e. with no backoff at all.
    """
    if attempt < 1:
        raise ValueError("attempt starts at 1")
    ceiling = min(MAX_BACKOFF, BASE_BACKOFF * (2 ** min(attempt - 1, 20)))
    r = (rng or random.SystemRandom()).random()
    floor = min(MIN_BACKOFF, ceiling).total_seconds()
    return timedelta(seconds=floor + (ceiling.total_seconds() - floor) * r)


def next_status_after_failure(attempts: int, max_attempts: int, *, retryable: bool) -> JobStatus:
    """A failed attempt is retried while attempts remain and the error is retryable."""
    return JobStatus.FAILED if retryable and attempts < max_attempts else JobStatus.DEAD
