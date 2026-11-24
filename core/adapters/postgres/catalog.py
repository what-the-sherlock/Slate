"""Catalog read models over Postgres, filtered by workspace and principals in SQL."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import RowMapping, text

from core.adapters.postgres.uow import UnitOfWork
from core.domain.errors import NotFound
from core.domain.ids import JobId, RunId, SourceId, UserId, WorkspaceId
from core.domain.principals import Principals
from core.ports.catalog import JobView, RunError, RunSummary, SourceSummary

_SOURCES = """
SELECT s.id, s.name, s.connector, s.data_class, s.visibility, s.status, s.last_synced_at,
       (SELECT count(*) FROM documents d
         WHERE d.source_id = s.id AND d.deleted_at IS NULL AND d.acl && CAST(:p AS text[])) AS docs,
       r.status AS run_status, r.error_summary AS run_error,
       (SELECT count(*) FROM jobs j
         WHERE j.workspace_id = s.workspace_id AND j.payload->>'source_id' = s.id::text
           AND j.status IN ('queued', 'running', 'failed')) AS pending
FROM sources s
LEFT JOIN LATERAL (
    SELECT status, error_summary FROM ingestion_runs
    WHERE source_id = s.id ORDER BY created_at DESC LIMIT 1
) r ON true
WHERE s.workspace_id = :ws AND s.visibility && CAST(:p AS text[])
ORDER BY s.name
"""


class PostgresCatalog:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def sources(self, ws: WorkspaceId, principals: Principals) -> list[SourceSummary]:
        async with self._uow() as s:
            rows = (
                (await s.execute(text(_SOURCES), {"ws": ws, "p": principals.as_list()}))
                .mappings()
                .all()
            )
        out: list[SourceSummary] = []
        for r in rows:
            visibility: Any = r["visibility"]  # reason: text[] decodes to list[str]
            out.append(
                SourceSummary(
                    id=SourceId(r["id"]),
                    name=r["name"],
                    connector=r["connector"],
                    data_class=r["data_class"],
                    visibility=tuple(str(v) for v in visibility),
                    status=r["status"],
                    documents=int(r["docs"]),
                    last_synced_at=r["last_synced_at"],
                    last_run_status=r["run_status"],
                    last_run_error=r["run_error"],
                    pending_jobs=int(r["pending"]),
                )
            )
        return out

    async def runs(
        self, ws: WorkspaceId, principals: Principals, source_id: SourceId, limit: int
    ) -> list[RunSummary]:
        async with self._uow() as s:
            visible = (
                await s.execute(
                    text(
                        "SELECT 1 FROM sources WHERE id = :s AND workspace_id = :ws"
                        " AND visibility && CAST(:p AS text[])"
                    ),
                    {"s": source_id, "ws": ws, "p": principals.as_list()},
                )
            ).first()
            if visible is None:
                raise NotFound("source not found")
            runs = (
                (
                    await s.execute(
                        text(
                            "SELECT id, kind, status, started_at, finished_at, docs_seen,"
                            " docs_changed, docs_failed, error_summary FROM ingestion_runs"
                            " WHERE source_id = :s ORDER BY created_at DESC LIMIT :n"
                        ),
                        {"s": source_id, "n": limit},
                    )
                )
                .mappings()
                .all()
            )
            ids = [r["id"] for r in runs]
            errors: Sequence[RowMapping] = []
            if ids:
                errors = (
                    (
                        await s.execute(
                            text(
                                "SELECT run_id, stage, message, retryable, external_id"
                                " FROM ingestion_errors WHERE run_id = ANY(CAST(:ids AS uuid[]))"
                                " ORDER BY created_at"
                            ),
                            {"ids": ids},
                        )
                    )
                    .mappings()
                    .all()
                )
        by_run: dict[UUID, list[RunError]] = {}
        for e in errors:
            by_run.setdefault(e["run_id"], []).append(
                RunError(e["stage"], e["message"], e["retryable"], e["external_id"])
            )
        return [
            RunSummary(
                id=RunId(r["id"]),
                kind=r["kind"],
                status=r["status"],
                started_at=r["started_at"],
                finished_at=r["finished_at"],
                docs_seen=r["docs_seen"],
                docs_changed=r["docs_changed"],
                docs_failed=r["docs_failed"],
                error_summary=r["error_summary"],
                errors=tuple(by_run.get(r["id"], [])),
            )
            for r in runs
        ]

    async def job(self, ws: WorkspaceId, job_id: JobId) -> JobView:
        async with self._uow() as s:
            r = (
                (
                    await s.execute(
                        text(
                            "SELECT id, status, attempts, last_error, payload->>'filename' AS fn,"
                            " payload->>'uploader' AS up FROM jobs"
                            " WHERE id = :id AND workspace_id = :ws"
                        ),
                        {"id": job_id, "ws": ws},
                    )
                )
                .mappings()
                .first()
            )
        if r is None:
            raise NotFound("job not found")
        return JobView(
            id=JobId(r["id"]),
            status=r["status"],
            attempts=r["attempts"],
            last_error=r["last_error"],
            filename=r["fn"],
            uploader=UserId(UUID(r["up"])) if r["up"] else None,
        )
