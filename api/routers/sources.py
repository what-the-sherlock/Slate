from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from api.deps import Container, get_container
from api.schemas.catalog import ActivityOut, JobOut, RunErrorOut, RunOut, SourceOut, SyncOut
from api.security import CurrentIdentity, MutatingIdentity
from core.domain.ids import JobId, SourceId
from core.domain.principals import PUBLIC
from core.domain.skips import describe_skips
from core.domain.source_status import Status, source_status
from core.ports.catalog import SourceSummary
from core.services.catalog import CatalogService
from core.services.source_actions import SourceActions

router = APIRouter(tags=["sources"])


def _catalog(container: Annotated[Container, Depends(get_container)]) -> CatalogService:
    return container.catalog


Catalog = Annotated[CatalogService, Depends(_catalog)]


def _actions(container: Annotated[Container, Depends(get_container)]) -> SourceActions:
    return container.source_actions


Actions = Annotated[SourceActions, Depends(_actions)]


def _status(s: SourceSummary, now: datetime) -> Status:
    """The badge and its reason, from raw facts (core.domain.source_status)."""
    return source_status(s.facts(), now)


def _visibility(s: SourceSummary) -> str:
    if PUBLIC in s.visibility:
        return "public"
    if any(v.startswith("workspace:") for v in s.visibility):
        return "workspace"
    return "only_me" if all(v.startswith("user:") for v in s.visibility) else "mixed"


@router.get("/sources", summary="Sources you can see, with status", response_model=list[SourceOut])
async def list_sources(identity: CurrentIdentity, catalog: Catalog) -> list[SourceOut]:
    now = datetime.now(UTC)
    out: list[SourceOut] = []
    for s in await catalog.sources(identity):
        st = _status(s, now)
        out.append(
            SourceOut(
                id=s.id,
                name=s.name,
                connector=s.connector,
                visibility=_visibility(s),  # pyright: ignore[reportArgumentType]
                data_class=s.data_class,  # pyright: ignore[reportArgumentType]
                status=st.badge.value,
                status_reason=st.reason,
                busy=st.busy,
                healthy=st.healthy,
                documents=s.documents,
                last_synced_at=s.last_synced_at,
                last_error=st.reason if not st.healthy else None,
                skipped_summary=describe_skips(s.run_skipped),
                shareable=s.created_by == identity.user_id and s.connector != "upload",
            )
        )
    return out


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
            docs_skipped=sum(r.skipped.values()),
            skipped=dict(r.skipped),
            skipped_summary=describe_skips(r.skipped),
        )
        for r in await catalog.runs(identity, SourceId(source_id), limit)
    ]


@router.get(
    "/runs",
    summary="Recent sync and upload activity across the sources you can see",
    response_model=list[ActivityOut],
)
async def activity(
    identity: CurrentIdentity, catalog: Catalog, limit: Annotated[int, Query(ge=1, le=100)] = 20
) -> list[ActivityOut]:
    return [
        ActivityOut(
            id=a.run.id, kind=a.run.kind, status=a.run.status, started_at=a.run.started_at,
            finished_at=a.run.finished_at, docs_seen=a.run.docs_seen,
            docs_changed=a.run.docs_changed, docs_failed=a.run.docs_failed,
            error_summary=a.run.error_summary, errors=[],
            source_id=a.source_id, source_name=a.source_name, connector=a.connector,
        )
        for a in await catalog.activity(identity, limit)
    ]  # fmt: skip


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


@router.post(
    "/sources/{source_id}/sync",
    summary="Sync a connected source now (GitHub repo, Discord server)",
    response_model=SyncOut,
)
async def sync_now(source_id: UUID, identity: MutatingIdentity, actions: Actions) -> SyncOut:
    result = await actions.sync_now(identity, SourceId(source_id))
    return SyncOut(
        job_id=result.job_id,
        status=result.status.value,
        reason=result.reason,
        skipped_summary=result.skipped,
    )


@router.delete(
    "/sources/{source_id}",
    summary="Disconnect a source and delete what it indexed",
    status_code=204,
)
async def disconnect(source_id: UUID, identity: MutatingIdentity, actions: Actions) -> None:
    await actions.disconnect(identity, SourceId(source_id))
