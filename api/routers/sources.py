from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from api.deps import Container, get_container
from api.schemas.catalog import JobOut, RunErrorOut, RunOut, SourceOut
from api.security import CurrentIdentity
from core.domain.ids import JobId, SourceId
from core.domain.principals import PUBLIC
from core.ports.catalog import SourceSummary
from core.services.catalog import CatalogService

router = APIRouter(tags=["sources"])


def _catalog(container: Annotated[Container, Depends(get_container)]) -> CatalogService:
    return container.catalog


Catalog = Annotated[CatalogService, Depends(_catalog)]


def _status(s: SourceSummary) -> str:
    if s.status == "paused":
        return "paused"
    if s.pending_jobs:
        return "syncing" if s.last_run_status == "running" else "queued"
    if s.last_run_status == "failed":
        return "failed"
    if s.last_run_status == "partial":
        return "partial"  # stopped early (budget, rate limit, items that failed): see last_error
    return "synced" if s.last_synced_at else "never"


def _visibility(s: SourceSummary) -> str:
    if PUBLIC in s.visibility:
        return "public"
    if any(v.startswith("workspace:") for v in s.visibility):
        return "workspace"
    return "only_me" if all(v.startswith("user:") for v in s.visibility) else "mixed"


@router.get("/sources", summary="Sources you can see, with status", response_model=list[SourceOut])
async def list_sources(identity: CurrentIdentity, catalog: Catalog) -> list[SourceOut]:
    return [
        SourceOut(
            id=s.id,
            name=s.name,
            connector=s.connector,
            visibility=_visibility(s),  # pyright: ignore[reportArgumentType]
            data_class=s.data_class,  # pyright: ignore[reportArgumentType]
            status=_status(s),  # pyright: ignore[reportArgumentType]
            documents=s.documents,
            last_synced_at=s.last_synced_at,
            last_error=s.last_run_error if s.last_run_status == "failed" else None,
        )
        for s in await catalog.sources(identity)
    ]


@router.get(
    "/sources/{source_id}/runs",
    summary="Recent ingestion runs and their errors",
    response_model=list[RunOut],
)
async def list_runs(
    source_id: UUID,
    identity: CurrentIdentity,
    catalog: Catalog,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[RunOut]:
    return [
        RunOut(
            id=r.id,
            kind=r.kind,
            status=r.status,
            started_at=r.started_at,
            finished_at=r.finished_at,
            docs_seen=r.docs_seen,
            docs_changed=r.docs_changed,
            docs_failed=r.docs_failed,
            error_summary=r.error_summary,
            errors=[
                RunErrorOut(stage=e.stage, message=e.message, retryable=e.retryable)
                for e in r.errors
            ],
        )
        for r in await catalog.runs(identity, SourceId(source_id), limit)
    ]


@router.get(
    "/jobs/{job_id}", summary="Processing status of one of your uploads", response_model=JobOut
)
async def get_job(job_id: UUID, identity: CurrentIdentity, catalog: Catalog) -> JobOut:
    job = await catalog.job(identity, JobId(job_id))
    return JobOut(
        id=job.id,
        status=job.status,  # pyright: ignore[reportArgumentType]
        attempts=job.attempts,
        error=job.last_error if job.status in {"dead", "failed"} else None,
        filename=job.filename,
    )
