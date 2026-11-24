import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest

from core.domain.errors import Conflict, ConnectorError, ValidationFailed
from core.domain.ids import JobId, WorkspaceId
from core.domain.jobs import STALE_AFTER, Job, JobStatus
from tests.fakes.jobs import FakeJobQueue
from tests.fakes.system import FakeClock
from tests.port_contracts.job_queue import PAST
from worker.runner import Handler, RunLimits, Worker

WS = WorkspaceId(uuid4())


async def ok(job: Job) -> None:
    return None


def raising(exc: Exception) -> Handler:
    async def handler(job: Job) -> None:
        raise exc

    return handler


async def _queue_with(*kinds: str) -> FakeJobQueue:
    q = FakeJobQueue()
    for i, kind in enumerate(kinds):
        await q.enqueue(WS, kind, {}, f"k{i}", run_after=PAST)
    return q


async def test_drains_queue_and_marks_success() -> None:
    q = await _queue_with("a", "a", "a")
    summary = await Worker(q, {"a": ok}, "w").run(RunLimits())
    assert summary.succeeded == 3
    assert all(j.status is JobStatus.SUCCEEDED for j in q.jobs.values())


async def test_once_processes_a_single_job() -> None:
    q = await _queue_with("a", "a")
    summary = await Worker(q, {"a": ok}, "w").run(RunLimits(max_jobs=1))
    assert summary.processed == 1


async def test_only_claims_kinds_it_has_handlers_for() -> None:
    q = await _queue_with("a", "unknown")
    summary = await Worker(q, {"a": ok}, "w").run(RunLimits())
    assert summary.processed == 1
    statuses = {j.kind: j.status for j in q.jobs.values()}
    assert statuses["unknown"] is JobStatus.QUEUED


async def test_retryable_app_error_schedules_retry() -> None:
    q = await _queue_with("a")
    err = ConnectorError("github 502", retryable=True, connector="github")
    summary = await Worker(q, {"a": raising(err)}, "w").run(RunLimits())
    assert summary.retrying == 1
    job = next(iter(q.jobs.values()))
    assert job.status is JobStatus.FAILED
    assert job.last_error == "github 502"


async def test_non_retryable_app_error_goes_dead() -> None:
    q = await _queue_with("a")
    summary = await Worker(q, {"a": raising(ValidationFailed("bad pdf"))}, "w").run(RunLimits())
    assert summary.dead == 1


async def test_unexpected_exception_is_retried_and_its_message_not_stored() -> None:
    q = await _queue_with("a")
    secret = RuntimeError("document text: salary is 100k")
    summary = await Worker(q, {"a": raising(secret)}, "w").run(RunLimits())
    assert summary.retrying == 1
    job = next(iter(q.jobs.values()))
    assert job.last_error == "unexpected RuntimeError"


async def test_one_failing_job_does_not_stop_the_others() -> None:
    q = await _queue_with("bad", "good", "good")
    handlers = {"bad": raising(RuntimeError("x")), "good": ok}
    summary = await Worker(q, handlers, "w").run(RunLimits())
    assert summary.succeeded == 2
    assert summary.retrying == 1


async def test_time_budget_stops_before_the_next_job() -> None:
    q = await _queue_with("a", "a", "a")
    ticks = iter([0.0, 0.0, 10.0, 99.0, 99.0, 99.0])
    worker = Worker(q, {"a": ok}, "w", monotonic=lambda: next(ticks))
    summary = await worker.run(RunLimits(max_seconds=50))
    assert summary.processed == 1
    assert summary.stopped_early


async def test_stop_request_finishes_current_job_then_exits() -> None:
    q = await _queue_with("a", "a")
    worker: Worker

    async def stop_after(job: Job) -> None:
        worker.request_stop()

    worker = Worker(q, {"a": stop_after}, "w")
    summary = await worker.run(RunLimits())
    assert summary.succeeded == 1
    assert summary.stopped_early


async def test_long_job_sends_heartbeats() -> None:
    q = await _queue_with("a")
    beats: list[object] = []
    original = q.heartbeat

    async def counting(job_id: JobId, worker_id: str) -> None:
        beats.append(job_id)
        await original(job_id, worker_id)

    q.heartbeat = counting

    async def slow(job: Job) -> None:
        await asyncio.sleep(0.05)

    await Worker(q, {"a": slow}, "w", heartbeat_every_s=0.01).run(RunLimits())
    assert len(beats) >= 2


async def test_reclaims_stale_jobs_before_starting() -> None:
    clock = FakeClock()
    q = FakeJobQueue(clock)
    await q.enqueue(WS, "a", {}, "stale", run_after=PAST)
    crashed = await q.claim("crashed-worker", ["a"])  # a previous run died holding this
    assert crashed is not None
    clock.advance(STALE_AFTER + timedelta(minutes=1))
    summary = await Worker(q, {"a": ok}, "w").run(RunLimits())
    assert summary.reclaimed == 1
    assert summary.succeeded == 1  # reclaimed, then processed in the same run
    assert q.jobs[crashed.id].status is JobStatus.SUCCEEDED


# ---------------------------------------------------------------- lost locks and blips
# (review findings: none of these may crash the worker or stop the remaining jobs)


async def test_conflict_on_complete_is_counted_lost_and_run_continues() -> None:
    q = await _queue_with("a", "a")
    calls = 0
    original = q.complete

    async def flaky_complete(job_id: JobId, worker_id: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise Conflict("job is not held by this worker")
        await original(job_id, worker_id)

    q.complete = flaky_complete
    summary = await Worker(q, {"a": ok}, "w").run(RunLimits())
    assert summary.lost == 1
    assert summary.succeeded == 1


async def test_db_error_while_recording_failure_does_not_crash() -> None:
    q = await _queue_with("a", "a")

    async def broken_fail(*args: object, **kwargs: object) -> Job:
        raise OSError("connection reset")

    q.fail = broken_fail
    handlers = {"a": raising(RuntimeError("boom"))}
    summary = await Worker(q, handlers, "w").run(RunLimits(max_jobs=2))
    assert summary.lost == 2


async def test_transient_heartbeat_error_keeps_heartbeating() -> None:
    q = await _queue_with("a")
    beats = 0
    original = q.heartbeat

    async def blip_then_ok(job_id: JobId, worker_id: str) -> None:
        nonlocal beats
        beats += 1
        if beats == 1:
            raise OSError("neon connection dropped")
        await original(job_id, worker_id)

    q.heartbeat = blip_then_ok

    async def slow(job: Job) -> None:
        await asyncio.sleep(0.06)

    summary = await Worker(q, {"a": slow}, "w", heartbeat_every_s=0.01).run(RunLimits())
    assert beats >= 3  # kept beating after the blip
    assert summary.succeeded == 1


async def test_lost_lock_cancels_the_handler() -> None:
    q = await _queue_with("a")
    finished = False

    async def stolen(job_id: JobId, worker_id: str) -> None:
        raise Conflict("job is not held by this worker")

    q.heartbeat = stolen

    async def long_job(job: Job) -> None:
        nonlocal finished
        await asyncio.sleep(5)
        finished = True

    summary = await Worker(q, {"a": long_job}, "w", heartbeat_every_s=0.01).run(RunLimits())
    assert summary.lost == 1
    assert not finished  # the handler was stopped, not left running


def test_requires_handlers() -> None:
    with pytest.raises(ValueError, match="at least one handler"):
        Worker(FakeJobQueue(), {}, "w")
