from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timedelta

from core.domain.errors import Conflict, NotFound
from core.domain.ids import JobId, WorkspaceId
from core.domain.jobs import Job, JobStatus, JsonObject, next_status_after_failure, retry_delay
from tests.fakes.system import FakeClock, SequentialIds

_RUNNABLE = {JobStatus.QUEUED, JobStatus.FAILED}


class FakeJobQueue:
    """In-memory JobQueue with the same semantics the Postgres adapter must have."""

    def __init__(self, clock: FakeClock | None = None, *, seed: int = 0) -> None:
        self.clock = clock or FakeClock()
        self._ids = SequentialIds()
        self._rng = random.Random(seed)
        self.jobs: dict[JobId, Job] = {}

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
        for job in self.jobs.values():
            if job.idempotency_key == idempotency_key:
                return job.id
        job_id = JobId(self._ids.new_uuid())
        now = self.clock.now()
        self.jobs[job_id] = Job(
            id=job_id,
            workspace_id=ws,
            kind=kind,
            payload=dict(payload),
            status=JobStatus.QUEUED,
            attempts=0,
            max_attempts=max_attempts,
            run_after=run_after or now,
            idempotency_key=idempotency_key,
            created_at=now,
        )
        return job_id

    async def claim(self, worker_id: str, kinds: Sequence[str]) -> Job | None:
        now = self.clock.now()
        due = [
            j
            for j in self.jobs.values()
            if j.status in _RUNNABLE and j.run_after <= now and j.kind in kinds
        ]
        if not due:
            return None
        job = min(due, key=lambda j: (j.run_after, j.created_at or now, j.id))
        claimed = replace(
            job,
            status=JobStatus.RUNNING,
            attempts=job.attempts + 1,
            locked_by=worker_id,
            heartbeat_at=now,
        )
        self.jobs[job.id] = claimed
        return claimed

    def _owned(self, job_id: JobId, worker_id: str) -> Job:
        job = self.jobs.get(job_id)
        if job is None:
            raise NotFound("job not found")
        if job.status is not JobStatus.RUNNING or job.locked_by != worker_id:
            raise Conflict("job is not held by this worker")
        return job

    async def heartbeat(self, job_id: JobId, worker_id: str) -> None:
        job = self._owned(job_id, worker_id)
        self.jobs[job_id] = replace(job, heartbeat_at=self.clock.now())

    async def complete(self, job_id: JobId, worker_id: str) -> None:
        job = self._owned(job_id, worker_id)
        self.jobs[job_id] = replace(
            job, status=JobStatus.SUCCEEDED, locked_by=None, last_error=None
        )

    async def fail(self, job_id: JobId, worker_id: str, error: str, *, retryable: bool) -> Job:
        job = self._owned(job_id, worker_id)
        status = next_status_after_failure(job.attempts, job.max_attempts, retryable=retryable)
        run_after = job.run_after
        if status is JobStatus.FAILED:
            run_after = self.clock.now() + retry_delay(job.attempts, self._rng)
        failed = replace(
            job, status=status, locked_by=None, last_error=error[:2000], run_after=run_after
        )
        self.jobs[job_id] = failed
        return failed

    async def reclaim_stale(self, older_than: timedelta) -> int:
        cutoff = self.clock.now() - older_than
        n = 0
        for job in list(self.jobs.values()):
            if job.status is JobStatus.RUNNING and job.heartbeat_at and job.heartbeat_at < cutoff:
                status = JobStatus.DEAD if job.attempts >= job.max_attempts else JobStatus.QUEUED
                self.jobs[job.id] = replace(
                    job,
                    status=status,
                    locked_by=None,
                    heartbeat_at=None,
                    last_error="worker heartbeat lost",
                )
                n += 1
        return n

    async def claim_by_id(self, job_id: JobId, worker_id: str) -> Job | None:
        job = self.jobs.get(job_id)
        if (
            job is None
            or job.status not in _RUNNABLE
            or job.run_after > self.clock.now()
            or job.attempts >= job.max_attempts
        ):
            return None
        claimed = replace(
            job,
            status=JobStatus.RUNNING,
            attempts=job.attempts + 1,
            locked_by=worker_id,
            heartbeat_at=self.clock.now(),
        )
        self.jobs[job_id] = claimed
        return claimed

    async def find_by_key(self, idempotency_key: str) -> Job | None:
        return next((j for j in self.jobs.values() if j.idempotency_key == idempotency_key), None)

    async def get(self, job_id: JobId) -> Job:
        job = self.jobs.get(job_id)
        if job is None:
            raise NotFound("job not found")
        return job
