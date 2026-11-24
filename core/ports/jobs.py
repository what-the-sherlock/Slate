"""Background work ports."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Protocol

from core.domain.ids import JobId, WorkspaceId
from core.domain.jobs import Job, JsonObject


class JobQueue(Protocol):
    async def enqueue(
        self,
        ws: WorkspaceId,
        kind: str,
        payload: JsonObject,
        idempotency_key: str,
        *,
        run_after: datetime | None = None,
        max_attempts: int = 5,
    ) -> JobId:
        """Add a job. Enqueuing an existing idempotency key returns the existing job's id."""
        ...

    async def claim(self, worker_id: str, kinds: Sequence[str]) -> Job | None:
        """Atomically take the oldest runnable job (queued, or failed and due).

        Concurrent workers never claim the same job (FOR UPDATE SKIP LOCKED).
        """
        ...

    async def heartbeat(self, job_id: JobId, worker_id: str) -> None:
        """Prove the worker is alive. Raises Conflict if the job is no longer ours."""
        ...

    async def complete(self, job_id: JobId, worker_id: str) -> None: ...

    async def fail(self, job_id: JobId, worker_id: str, error: str, *, retryable: bool) -> Job:
        """Record a failure: schedule a retry with backoff, or mark dead."""
        ...

    async def reclaim_stale(self, older_than: timedelta) -> int:
        """Requeue running jobs whose heartbeat is older than ``older_than``."""
        ...

    async def get(self, job_id: JobId) -> Job:
        """Raises NotFound."""
        ...

    async def find_by_key(self, idempotency_key: str) -> Job | None: ...


class WorkerTrigger(Protocol):
    async def trigger(self, reason: str) -> None:
        """Ask the worker to run soon (GitHub repository_dispatch). Best effort."""
        ...
