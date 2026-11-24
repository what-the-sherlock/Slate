"""End to end on Neon: enqueue a noop job, run the real worker CLI, see it succeed."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from sqlalchemy import text

from core.adapters.postgres.jobs import PostgresJobQueue
from core.adapters.postgres.uow import UnitOfWork
from core.domain.ids import WorkspaceId
from core.domain.jobs import JobStatus
from worker.handlers import NOOP

ROOT = Path(__file__).resolve().parents[2]


async def test_worker_cli_processes_a_noop_job(uow: UnitOfWork) -> None:
    async with uow() as s:
        ws = (
            await s.execute(
                text(
                    "INSERT INTO workspaces (name, slug)"
                    " VALUES ('worker-e2e', 'worker-' || gen_random_uuid()) RETURNING id"
                )
            )
        ).scalar_one()
    try:
        queue = PostgresJobQueue(uow)
        job_id = await queue.enqueue(WorkspaceId(ws), NOOP, {}, f"e2e-{uuid4()}")
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "worker", "run", "--max-seconds", "120"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        assert result.returncode == 0, result.stderr[-2000:]
        assert (await queue.get(job_id)).status is JobStatus.SUCCEEDED
    finally:
        async with uow() as s:
            await s.execute(text("DELETE FROM workspaces WHERE id = :id"), {"id": ws})
