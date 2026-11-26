"""Process a just-queued job immediately, inside the API request (M1 slice 5).

Small uploads shouldn't wait for the hourly worker. This claims that specific job (so a
worker can't run it at the same time), runs the same handler the worker would, and
records the outcome through the queue: the job table stays the single source of truth.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from enum import StrEnum

import structlog

from core.domain.errors import AppError
from core.domain.ids import JobId
from core.domain.jobs import Job, JobStatus
from core.ports.jobs import JobQueue

log = structlog.get_logger("inline")

INLINE_MAX_BYTES = 1024 * 1024  # bigger files go to the worker
INLINE_TIMEOUT_S = 120.0  # well inside Vercel's 300 s function limit


class InlineOutcome(StrEnum):
    DONE = "done"
    DEFERRED = "deferred"  # left for the worker (busy, or a retryable problem)


class InlineRunner:
    def __init__(
        self,
        queue: JobQueue,
        handler: Callable[[Job], Awaitable[None]],
        *,
        runner_id: str,
        kinds: frozenset[str] | None = None,
    ) -> None:
        self._queue = queue
        self._handler = handler
        self._id = runner_id
        self._kinds = kinds  # the job kinds this handler understands (None: any)

    async def run(self, job_id: JobId) -> InlineOutcome:
        """Raises the handler's AppError when the failure is final (e.g. a bad file), so the
        caller can show the reason; retryable failures are handed back to the worker.
        """
        if self._kinds is not None and (await self._queue.get(job_id)).kind not in self._kinds:
            return InlineOutcome.DEFERRED  # not this handler's job: the worker routes it
        job = await self._queue.claim_by_id(job_id, self._id)
        if job is None:
            return InlineOutcome.DEFERRED  # a worker got there first
        try:
            async with asyncio.timeout(INLINE_TIMEOUT_S):
                await self._handler(job)
        except TimeoutError:
            await self._queue.fail(job.id, self._id, "inline processing timed out", retryable=True)
            log.info("inline_timeout_deferred", job_id=str(job.id))
            return InlineOutcome.DEFERRED
        except AppError as e:
            failed = await self._queue.fail(job.id, self._id, e.detail, retryable=e.retryable)
            if failed.status is JobStatus.DEAD:
                raise
            log.info("inline_deferred", job_id=str(job.id), error_code=e.code)
            return InlineOutcome.DEFERRED
        except Exception as e:  # unexpected: let the worker retry it
            await self._queue.fail(
                job.id, self._id, f"unexpected {type(e).__name__}", retryable=True
            )
            log.error("inline_crashed", job_id=str(job.id), error_type=type(e).__name__)
            return InlineOutcome.DEFERRED
        await self._queue.complete(job.id, self._id)
        return InlineOutcome.DONE
