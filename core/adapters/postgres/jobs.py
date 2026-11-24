"""JobQueue over Postgres (ADR-0007).

Claiming uses ``FOR UPDATE SKIP LOCKED``: concurrent workers each lock a different
row and never wait on, or double-claim, a job another worker holds. Times come from
the database clock (``now()``) so workers with skewed clocks agree on what is due.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.adapters.postgres.uow import UnitOfWork
from core.domain.errors import Conflict, NotFound
from core.domain.ids import JobId, WorkspaceId
from core.domain.jobs import Job, JobStatus, JsonObject, next_status_after_failure, retry_delay

_COLUMNS = (
    "id, workspace_id, kind, payload, status, attempts, max_attempts, run_after, "
    "idempotency_key, locked_by, heartbeat_at, last_error, created_at"
)

_CLAIM = f"""
WITH next AS (
    SELECT id FROM jobs
    WHERE status IN ('queued', 'failed') AND run_after <= now() AND kind = ANY(:kinds)
    ORDER BY run_after, created_at, id
    LIMIT 1
    FOR UPDATE SKIP LOCKED
)
UPDATE jobs j
SET status = 'running', attempts = j.attempts + 1, locked_by = :worker,
    locked_at = now(), heartbeat_at = now()
FROM next
WHERE j.id = next.id
RETURNING {", ".join("j." + c.strip() for c in _COLUMNS.split(","))}
"""  # noqa: S608 (column list is a module constant)


_FAIL = f"""
UPDATE jobs SET status = :status, locked_by = NULL, last_error = :err,
    run_after = CASE WHEN :status = 'failed'
        THEN now() + make_interval(secs => :delay) ELSE run_after END,
    finished_at = CASE WHEN :status = 'dead' THEN now() END
WHERE id = :id
RETURNING {_COLUMNS}
"""  # noqa: S608 (column list is a module constant)


def _to_job(row: RowMapping) -> Job:
    payload: Any = row["payload"]  # reason: jsonb decodes to arbitrary JSON
    return Job(
        id=JobId(row["id"]),
        workspace_id=WorkspaceId(row["workspace_id"]),
        kind=row["kind"],
        payload=dict(payload) if isinstance(payload, dict) else {},  # pyright: ignore[reportUnknownArgumentType]
        status=JobStatus(row["status"]),
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        run_after=row["run_after"],
        idempotency_key=row["idempotency_key"],
        locked_by=row["locked_by"],
        heartbeat_at=row["heartbeat_at"],
        last_error=row["last_error"],
        created_at=row["created_at"],
    )


class PostgresJobQueue:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

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
        async with self._uow() as s:
            # ON CONFLICT DO NOTHING + re-select: an existing key returns the original job.
            row = (
                await s.execute(
                    text(
                        "INSERT INTO jobs (workspace_id, kind, payload, idempotency_key,"
                        " run_after, max_attempts)"
                        " VALUES (:ws, :kind, CAST(:payload AS jsonb), :key,"
                        " coalesce(:run_after, now()), :max)"
                        " ON CONFLICT (idempotency_key) DO NOTHING RETURNING id"
                    ),
                    {
                        "ws": ws,
                        "kind": kind,
                        "payload": json.dumps(payload),
                        "key": idempotency_key,
                        "run_after": run_after,
                        "max": max_attempts,
                    },
                )
            ).first()
            if row is None:
                row = (
                    await s.execute(
                        text("SELECT id FROM jobs WHERE idempotency_key = :key"),
                        {"key": idempotency_key},
                    )
                ).one()
            return JobId(row[0])

    async def claim(self, worker_id: str, kinds: Sequence[str]) -> Job | None:
        async with self._uow() as s:
            row = (
                (await s.execute(text(_CLAIM), {"worker": worker_id, "kinds": list(kinds)}))
                .mappings()
                .first()
            )
            return None if row is None else _to_job(row)

    async def _require_owned(self, s: AsyncSession, job_id: JobId, worker_id: str) -> Job:
        row = (
            (
                await s.execute(
                    text(f"SELECT {_COLUMNS} FROM jobs WHERE id = :id FOR UPDATE"),  # noqa: S608
                    {"id": job_id},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise NotFound("job not found")
        job = _to_job(row)
        if job.status is not JobStatus.RUNNING or job.locked_by != worker_id:
            raise Conflict("job is not held by this worker")
        return job

    async def heartbeat(self, job_id: JobId, worker_id: str) -> None:
        async with self._uow() as s:
            await self._require_owned(s, job_id, worker_id)
            await s.execute(
                text("UPDATE jobs SET heartbeat_at = now() WHERE id = :id"), {"id": job_id}
            )

    async def complete(self, job_id: JobId, worker_id: str) -> None:
        async with self._uow() as s:
            await self._require_owned(s, job_id, worker_id)
            await s.execute(
                text(
                    "UPDATE jobs SET status = 'succeeded', locked_by = NULL, finished_at = now()"
                    " WHERE id = :id"
                ),
                {"id": job_id},
            )

    async def fail(self, job_id: JobId, worker_id: str, error: str, *, retryable: bool) -> Job:
        async with self._uow() as s:
            job = await self._require_owned(s, job_id, worker_id)
            status = next_status_after_failure(job.attempts, job.max_attempts, retryable=retryable)
            delay = retry_delay(job.attempts) if status is JobStatus.FAILED else timedelta(0)
            row = (
                (
                    await s.execute(
                        text(_FAIL),
                        {
                            "status": status.value,
                            "err": error[:2000],
                            "delay": delay.total_seconds(),
                            "id": job_id,
                        },
                    )
                )
                .mappings()
                .one()
            )
            return _to_job(row)

    async def reclaim_stale(self, older_than: timedelta) -> int:
        """Requeue running jobs whose worker stopped heartbeating.

        A job that has already used all its attempts is marked dead instead, so a job
        that reliably kills its worker can't loop forever.
        """
        async with self._uow() as s:
            result = await s.execute(
                text(
                    "UPDATE jobs SET locked_by = NULL, locked_at = NULL, heartbeat_at = NULL,"
                    " status = CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'queued' END,"
                    " finished_at = CASE WHEN attempts >= max_attempts THEN now() END,"
                    " last_error = 'worker heartbeat lost'"
                    " WHERE status = 'running'"
                    " AND heartbeat_at < now() - make_interval(secs => :secs)"
                    " RETURNING id"
                ),
                {"secs": older_than.total_seconds()},
            )
            return len(result.all())

    async def find_by_key(self, idempotency_key: str) -> Job | None:
        async with self._uow() as s:
            result = await s.execute(
                text(f"SELECT {_COLUMNS} FROM jobs WHERE idempotency_key = :k"),  # noqa: S608
                {"k": idempotency_key},
            )
            row = result.mappings().first()
        return None if row is None else _to_job(row)

    async def get(self, job_id: JobId) -> Job:
        async with self._uow() as s:
            row = (
                (
                    await s.execute(
                        text(f"SELECT {_COLUMNS} FROM jobs WHERE id = :id"),  # noqa: S608
                        {"id": job_id},
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise NotFound("job not found")
        return _to_job(row)
