from uuid import uuid4

import pytest

from core.domain.errors import UpstreamUnavailable, ValidationFailed
from core.domain.ids import WorkspaceId
from core.domain.jobs import Job, JobStatus
from core.services.inline import InlineOutcome, InlineRunner
from tests.fakes.jobs import FakeJobQueue
from tests.fakes.system import FakeClock
from tests.port_contracts.job_queue import PAST

WS = WorkspaceId(uuid4())


async def setup(handler):
    q = FakeJobQueue()
    job_id = await q.enqueue(WS, "process_upload", {}, "k", run_after=PAST)
    return q, job_id, InlineRunner(q, handler, runner_id="api")


async def test_success_completes_the_job() -> None:
    async def ok(job: Job) -> None:
        return None

    q, job_id, runner = await setup(ok)
    assert await runner.run(job_id) is InlineOutcome.DONE
    assert (await q.get(job_id)).status is JobStatus.SUCCEEDED


async def test_retryable_failure_is_left_for_the_worker() -> None:
    async def flaky(job: Job) -> None:
        raise UpstreamUnavailable("openai down")

    q, job_id, runner = await setup(flaky)
    assert await runner.run(job_id) is InlineOutcome.DEFERRED
    assert (await q.get(job_id)).status is JobStatus.FAILED  # will retry with backoff


async def test_final_failure_is_raised_so_the_user_sees_why() -> None:
    async def bad(job: Job) -> None:
        raise ValidationFailed("No text found in this file.")

    q, job_id, runner = await setup(bad)
    with pytest.raises(ValidationFailed, match="No text"):
        await runner.run(job_id)
    assert (await q.get(job_id)).status is JobStatus.DEAD


async def test_a_job_the_worker_already_holds_is_not_run_twice() -> None:
    calls = 0

    async def count(job: Job) -> None:
        nonlocal calls
        calls += 1

    q, job_id, runner = await setup(count)
    assert await q.claim("worker", ["process_upload"]) is not None
    assert await runner.run(job_id) is InlineOutcome.DEFERRED
    assert calls == 0


async def test_unexpected_crash_is_reported_failed_not_raised_and_still_retried() -> None:
    async def boom(job: Job) -> None:
        raise RuntimeError("bug with document text")

    q, job_id, runner = await setup(boom)
    assert await runner.run(job_id) is InlineOutcome.FAILED  # never "continues in background"
    job = await q.get(job_id)
    assert job.status is JobStatus.FAILED  # the worker retries it
    assert job.last_error == "unexpected RuntimeError"  # type only, never the message


async def test_runner_leaves_jobs_of_other_kinds_to_the_worker() -> None:
    clock = FakeClock()
    queue = FakeJobQueue(clock)
    job = await queue.enqueue(WorkspaceId(uuid4()), "import_mbox", {}, "k-other")
    ran: list[object] = []

    async def handler(j: object) -> None:
        ran.append(j)

    runner = InlineRunner(queue, handler, runner_id="t", kinds=frozenset({"process_upload"}))
    assert await runner.run(job) is InlineOutcome.DEFERRED
    assert ran == []
    assert (await queue.get(job)).status.value == "queued"  # untouched, still claimable
