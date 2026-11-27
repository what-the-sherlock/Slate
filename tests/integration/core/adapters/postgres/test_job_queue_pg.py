"""The Postgres JobQueue passes the same contract as the in-memory fake."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from sqlalchemy import text

from core.adapters.postgres.jobs import PostgresJobQueue
from core.adapters.postgres.uow import UnitOfWork
from core.domain.ids import WorkspaceId
from core.domain.jobs import STALE_AFTER, JobStatus
from tests.port_contracts.job_queue import PAST, JobQueueContract


@pytest.fixture
async def pg_workspace(uow: UnitOfWork) -> AsyncIterator[WorkspaceId]:
    """A throwaway workspace; deleting it cascades to every job the test created."""
    async with uow() as s:
        ws = (
            await s.execute(
                text(
                    "INSERT INTO workspaces (name, slug)"
                    " VALUES ('jobs-test', 'jobs-' || gen_random_uuid()) RETURNING id"
                )
            )
        ).scalar_one()
    yield WorkspaceId(ws)
    async with uow() as s:
        await s.execute(text("DELETE FROM workspaces WHERE id = :id"), {"id": ws})


class TestPostgresJobQueueContract(JobQueueContract):
    @pytest.fixture
    def ws(self, pg_workspace: WorkspaceId) -> WorkspaceId:
        return pg_workspace

    @pytest.fixture
    def queue(self, uow: UnitOfWork) -> PostgresJobQueue:
        return PostgresJobQueue(uow)


async def _backdate_heartbeat(uow: UnitOfWork, job_id: object) -> None:
    async with uow() as s:
        await s.execute(
            text("UPDATE jobs SET heartbeat_at = now() - interval '1 hour' WHERE id = :id"),
            {"id": job_id},
        )


async def test_reclaim_requeues_a_job_whose_worker_died(
    uow: UnitOfWork, pg_workspace: WorkspaceId
) -> None:
    q = PostgresJobQueue(uow)
    kind = f"reclaim_{pg_workspace.hex[:8]}"
    await q.enqueue(pg_workspace, kind, {}, f"k-{pg_workspace}", run_after=PAST)
    job = await q.claim("dead-worker", [kind])
    assert job is not None
    await _backdate_heartbeat(uow, job.id)
    assert await q.reclaim_stale(STALE_AFTER) >= 1
    reclaimed = await q.get(job.id)
    assert reclaimed.status is JobStatus.QUEUED
    assert reclaimed.last_error == "worker heartbeat lost"
    again = await q.claim("new-worker", [kind])
    assert again is not None
    assert again.attempts == 2


async def test_reclaim_marks_dead_when_attempts_exhausted(
    uow: UnitOfWork, pg_workspace: WorkspaceId
) -> None:
    q = PostgresJobQueue(uow)
    kind = f"poison_{pg_workspace.hex[:8]}"
    await q.enqueue(pg_workspace, kind, {}, f"p-{pg_workspace}", run_after=PAST, max_attempts=1)
    job = await q.claim("w", [kind])
    assert job is not None
    await _backdate_heartbeat(uow, job.id)
    await q.reclaim_stale(timedelta(minutes=10))
    assert (await q.get(job.id)).status is JobStatus.DEAD


async def test_success_after_a_failed_attempt_clears_the_old_error(
    uow: UnitOfWork, pg_workspace: WorkspaceId
) -> None:
    q = PostgresJobQueue(uow)
    kind = f"clear_{pg_workspace.hex[:8]}"
    await q.enqueue(pg_workspace, kind, {}, f"c-{pg_workspace}", run_after=PAST)
    job = await q.claim("w1", [kind])
    assert job is not None
    await q.fail(job.id, "w1", "unexpected TypeError", retryable=True)
    async with uow() as s:  # make the retry due now
        await s.execute(
            text("UPDATE jobs SET run_after = now() - interval '1 second' WHERE id = :id"),
            {"id": job.id},
        )
    again = await q.claim("w1", [kind])
    assert again is not None
    await q.complete(again.id, "w1")
    done = await q.get(again.id)
    assert (done.status, done.last_error) == (JobStatus.SUCCEEDED, None)
