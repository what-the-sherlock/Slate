"""Behaviour every JobQueue adapter must have.

Subclass ``JobQueueContract`` and provide a ``queue`` fixture. The fake runs it in
unit tests; the Postgres adapter runs the same class in integration tests, so the
two implementations can't drift apart. Tests avoid controlling time: they use
``run_after`` in the past or future instead, which works against a real database.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from core.domain.errors import Conflict, NotFound
from core.domain.ids import JobId, WorkspaceId
from core.domain.jobs import JobStatus
from core.ports.jobs import JobQueue

PAST = datetime(2020, 1, 1, tzinfo=UTC)
FUTURE = datetime(2099, 1, 1, tzinfo=UTC)
KIND = "contract_test"


def _key() -> str:
    return f"contract:{uuid4()}"


class JobQueueContract:
    @pytest.fixture
    def ws(self) -> WorkspaceId:
        raise NotImplementedError

    async def test_enqueue_is_idempotent_on_key(self, queue: JobQueue, ws: WorkspaceId) -> None:
        key = _key()
        a = await queue.enqueue(ws, KIND, {"n": 1}, key, run_after=PAST)
        b = await queue.enqueue(ws, KIND, {"n": 2}, key, run_after=PAST)
        assert a == b
        assert (await queue.get(a)).payload == {"n": 1}

    async def test_claim_returns_oldest_due_job_and_marks_running(
        self, queue: JobQueue, ws: WorkspaceId
    ) -> None:
        older = await queue.enqueue(ws, KIND, {}, _key(), run_after=PAST)
        await queue.enqueue(ws, KIND, {}, _key(), run_after=PAST + timedelta(days=1))
        job = await queue.claim("w1", [KIND])
        assert job is not None
        assert job.id == older
        assert job.status is JobStatus.RUNNING
        assert job.attempts == 1
        assert job.locked_by == "w1"

    async def test_claim_skips_future_and_other_kinds(
        self, queue: JobQueue, ws: WorkspaceId
    ) -> None:
        await queue.enqueue(ws, KIND, {}, _key(), run_after=FUTURE)
        await queue.enqueue(ws, "other_kind", {}, _key(), run_after=PAST)
        assert await queue.claim("w1", [KIND]) is None

    async def test_concurrent_claims_never_return_the_same_job(
        self, queue: JobQueue, ws: WorkspaceId
    ) -> None:
        for _ in range(3):
            await queue.enqueue(ws, KIND, {}, _key(), run_after=PAST)
        claimed = await asyncio.gather(*(queue.claim(f"w{i}", [KIND]) for i in range(5)))
        ids = [j.id for j in claimed if j is not None]
        assert len(ids) == 3
        assert len(set(ids)) == 3

    async def test_complete_marks_succeeded(self, queue: JobQueue, ws: WorkspaceId) -> None:
        await queue.enqueue(ws, KIND, {}, _key(), run_after=PAST)
        job = await queue.claim("w1", [KIND])
        assert job is not None
        await queue.complete(job.id, "w1")
        assert (await queue.get(job.id)).status is JobStatus.SUCCEEDED

    async def test_only_the_owner_can_heartbeat_or_complete(
        self, queue: JobQueue, ws: WorkspaceId
    ) -> None:
        await queue.enqueue(ws, KIND, {}, _key(), run_after=PAST)
        job = await queue.claim("w1", [KIND])
        assert job is not None
        with pytest.raises(Conflict):
            await queue.heartbeat(job.id, "intruder")
        with pytest.raises(Conflict):
            await queue.complete(job.id, "intruder")

    async def test_retryable_failure_schedules_retry_with_backoff(
        self, queue: JobQueue, ws: WorkspaceId
    ) -> None:
        await queue.enqueue(ws, KIND, {}, _key(), run_after=PAST)
        job = await queue.claim("w1", [KIND])
        assert job is not None
        failed = await queue.fail(job.id, "w1", "timeout", retryable=True)
        assert failed.status is JobStatus.FAILED
        assert failed.last_error == "timeout"
        assert failed.run_after > PAST
        assert failed.locked_by is None

    async def test_non_retryable_failure_is_dead(self, queue: JobQueue, ws: WorkspaceId) -> None:
        await queue.enqueue(ws, KIND, {}, _key(), run_after=PAST)
        job = await queue.claim("w1", [KIND])
        assert job is not None
        assert (
            await queue.fail(job.id, "w1", "bad input", retryable=False)
        ).status is JobStatus.DEAD

    async def test_exhausted_attempts_become_dead_and_are_never_claimed(
        self, queue: JobQueue, ws: WorkspaceId
    ) -> None:
        await queue.enqueue(ws, KIND, {}, _key(), run_after=PAST, max_attempts=1)
        job = await queue.claim("w1", [KIND])
        assert job is not None
        assert (await queue.fail(job.id, "w1", "x", retryable=True)).status is JobStatus.DEAD
        assert await queue.claim("w1", [KIND]) is None

    async def test_get_unknown_job_raises_not_found(self, queue: JobQueue) -> None:
        with pytest.raises(NotFound):
            await queue.get(JobId(uuid4()))
