"""Job handlers by kind. M0 ships one no-op; ingestion handlers arrive in M1."""

from __future__ import annotations

import structlog

from core.domain.jobs import Job

log = structlog.get_logger("worker.handlers")

NOOP = "noop"


async def noop(job: Job) -> None:
    """Proves the pipeline end to end: enqueue -> claim -> run -> succeed."""
    log.info("noop_handled", job_id=str(job.id))
