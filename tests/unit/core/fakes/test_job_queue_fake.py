from datetime import timedelta
from uuid import uuid4

import pytest

from core.domain.ids import WorkspaceId
from core.domain.jobs import STALE_AFTER, JobStatus
from tests.fakes.jobs import FakeJobQueue
from tests.fakes.system import FakeClock
from tests.port_contracts.job_queue import KIND, PAST, JobQueueContract


class TestFakeJobQueueContract(JobQueueContract):
    @pytest.fixture
    def ws(self) -> WorkspaceId:
        return WorkspaceId(uuid4())

    @pytest.fixture
    def queue(self) -> FakeJobQueue:
        return FakeJobQueue()


# Time-dependent behaviour: only testable with a controllable clock.


async def test_failed_job_is_claimable_again_once_backoff_elapses() -> None:
    clock = FakeClock()
    q = FakeJobQueue(clock)
    ws = WorkspaceId(uuid4())
    await q.enqueue(ws, KIND, {}, "k", run_after=PAST)
    job = await q.claim("w1", [KIND])
    assert job is not None
    await q.fail(job.id, "w1", "boom", retryable=True)
    assert await q.claim("w1", [KIND]) is None  # still backing off
    clock.advance(timedelta(hours=2))
    retried = await q.claim("w1", [KIND])
    assert retried is not None
    assert retried.attempts == 2


async def test_reclaim_stale_requeues_jobs_with_old_heartbeats() -> None:
    clock = FakeClock()
    q = FakeJobQueue(clock)
    ws = WorkspaceId(uuid4())
    await q.enqueue(ws, KIND, {}, "k", run_after=PAST)
    job = await q.claim("w1", [KIND])
    assert job is not None
    clock.advance(STALE_AFTER + timedelta(seconds=1))
    assert await q.reclaim_stale(STALE_AFTER) == 1
    assert (await q.get(job.id)).status is JobStatus.QUEUED


async def test_heartbeat_keeps_a_job_from_being_reclaimed() -> None:
    clock = FakeClock()
    q = FakeJobQueue(clock)
    ws = WorkspaceId(uuid4())
    await q.enqueue(ws, KIND, {}, "k", run_after=PAST)
    job = await q.claim("w1", [KIND])
    assert job is not None
    clock.advance(STALE_AFTER - timedelta(minutes=1))
    await q.heartbeat(job.id, "w1")
    clock.advance(timedelta(minutes=5))
    assert await q.reclaim_stale(STALE_AFTER) == 0
