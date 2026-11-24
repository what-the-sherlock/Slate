"""The worker loop: claim a job, run its handler, record the outcome. Pure over ports.

Failure policy (ARCHITECTURE §7):
* a typed ``AppError`` fails the job, retrying only if the error says it's retryable;
* any other exception is unexpected, so it is retried (it may be transient), and it is
  logged by type only, because worker logs on a public repo are public (SECURITY.md);
* a background heartbeat proves the job is still alive. If the heartbeat finds the
  lock was lost (the job was reclaimed), the handler is cancelled so two workers never
  keep working on one job;
* nothing that happens to one job can stop the run: errors while *recording* an
  outcome are logged and the job is left for the next run's reclaim.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import structlog

from core.domain.errors import AppError, Conflict, NotFound
from core.domain.jobs import STALE_AFTER, Job, JobStatus
from core.ports.jobs import JobQueue

log = structlog.get_logger("worker")

Handler = Callable[[Job], Coroutine[Any, Any, None]]  # reason: coroutine type params
HEARTBEAT_EVERY_S = 60.0


class Outcome(StrEnum):
    SUCCEEDED = "succeeded"
    RETRYING = "retrying"
    DEAD = "dead"
    LOST = "lost"  # lock lost or outcome couldn't be recorded; next run's reclaim recovers it


@dataclass(frozen=True, slots=True)
class RunLimits:
    max_jobs: int | None = None  # None: drain the queue
    max_seconds: float = 5 * 3600  # checked between jobs; Actions' hard limit is 6 h


@dataclass(slots=True)
class RunSummary:
    reclaimed: int = 0
    succeeded: int = 0
    retrying: int = 0
    dead: int = 0
    lost: int = 0
    stopped_early: bool = False
    by_kind: dict[str, int] = field(default_factory=dict[str, int])

    @property
    def processed(self) -> int:
        return self.succeeded + self.retrying + self.dead + self.lost

    def count(self, outcome: Outcome) -> None:
        match outcome:
            case Outcome.SUCCEEDED:
                self.succeeded += 1
            case Outcome.RETRYING:
                self.retrying += 1
            case Outcome.DEAD:
                self.dead += 1
            case Outcome.LOST:
                self.lost += 1


class Worker:
    def __init__(
        self,
        queue: JobQueue,
        handlers: Mapping[str, Handler],
        worker_id: str,
        *,
        heartbeat_every_s: float = HEARTBEAT_EVERY_S,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not handlers:
            raise ValueError("a worker needs at least one handler")
        self._queue = queue
        self._handlers = dict(handlers)
        self._id = worker_id
        self._heartbeat_every_s = heartbeat_every_s
        self._monotonic = monotonic
        self._stop = asyncio.Event()

    def request_stop(self) -> None:
        """Finish the current job, then exit (used on SIGTERM / Ctrl+C)."""
        self._stop.set()

    async def run(self, limits: RunLimits) -> RunSummary:
        summary = RunSummary(reclaimed=await self._queue.reclaim_stale(STALE_AFTER))
        started = self._monotonic()
        while True:
            if self._stop.is_set() or self._monotonic() - started >= limits.max_seconds:
                summary.stopped_early = True
                break
            if limits.max_jobs is not None and summary.processed >= limits.max_jobs:
                break
            job = await self._queue.claim(self._id, list(self._handlers))
            if job is None:
                break
            summary.count(await self._process(job))
            summary.by_kind[job.kind] = summary.by_kind.get(job.kind, 0) + 1
        log.info(
            "worker_run_finished",
            reclaimed=summary.reclaimed,
            succeeded=summary.succeeded,
            retrying=summary.retrying,
            dead=summary.dead,
            lost=summary.lost,
            stopped_early=summary.stopped_early,
        )
        return summary

    async def _process(self, job: Job) -> Outcome:
        bound = log.bind(job_id=str(job.id), kind=job.kind, attempt=job.attempts)
        started = self._monotonic()
        lock_lost = asyncio.Event()
        work = asyncio.create_task(self._handlers[job.kind](job))
        heartbeat = asyncio.create_task(self._heartbeat(job, work, lock_lost))
        try:
            try:
                await work
            except asyncio.CancelledError:
                if lock_lost.is_set() and not _being_cancelled():
                    bound.warning("job_lost_lock")
                    return Outcome.LOST
                # The worker itself is shutting down: give the job back, then propagate.
                await asyncio.shield(self._fail(job, "worker cancelled", retryable=True))
                raise
            except AppError as e:
                bound.warning("job_failed", error_code=e.code)
                return await self._fail(job, e.detail, retryable=e.retryable)
            except Exception as e:  # job boundary: record, never crash the worker
                bound.error("job_crashed", error_type=type(e).__name__)
                return await self._fail(job, f"unexpected {type(e).__name__}", retryable=True)
            outcome = await self._record(self._queue.complete(job.id, self._id), job)
            if outcome is Outcome.SUCCEEDED:
                bound.info("job_succeeded", duration_ms=round((self._monotonic() - started) * 1000))
            return outcome
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat

    async def _fail(self, job: Job, error: str, *, retryable: bool) -> Outcome:
        async def fail() -> Outcome:
            failed = await self._queue.fail(job.id, self._id, error, retryable=retryable)
            return Outcome.RETRYING if failed.status is JobStatus.FAILED else Outcome.DEAD

        try:
            return await fail()
        except (Conflict, NotFound):
            log.warning("job_lost_lock", job_id=str(job.id))
        except Exception as e:  # DB blip: the next run's reclaim recovers the job
            log.error("job_outcome_not_recorded", job_id=str(job.id), error_type=type(e).__name__)
        return Outcome.LOST

    async def _record(self, call: Awaitable[None], job: Job) -> Outcome:
        try:
            await call
        except (Conflict, NotFound):
            log.warning("job_lost_lock", job_id=str(job.id))
            return Outcome.LOST
        except Exception as e:
            log.error("job_outcome_not_recorded", job_id=str(job.id), error_type=type(e).__name__)
            return Outcome.LOST
        return Outcome.SUCCEEDED

    async def _heartbeat(
        self, job: Job, work: asyncio.Task[None], lock_lost: asyncio.Event
    ) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_every_s)
            try:
                await self._queue.heartbeat(job.id, self._id)
            except (Conflict, NotFound):
                # Someone reclaimed the job: stop working on it.
                lock_lost.set()
                work.cancel()
                return
            except Exception as e:  # transient (e.g. Neon connection drop): keep beating
                log.warning("heartbeat_failed", job_id=str(job.id), error_type=type(e).__name__)


def _being_cancelled() -> bool:
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0
