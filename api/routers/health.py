from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response

from api.deps import Container, get_container
from api.schemas.health import HealthResponse, ReadinessResponse
from core.services.health import check_readiness

VERSION = "0.1.0"
router = APIRouter(tags=["health"])


@router.get("/healthz", summary="Liveness", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    """The process is up. Never touches the database, so it stays cheap and wake-free."""
    return HealthResponse(version=VERSION)


@router.get(
    "/readyz",
    summary="Readiness",
    response_model=ReadinessResponse,
    responses={503: {"model": ReadinessResponse, "description": "Not ready"}},
)
async def readyz(
    response: Response, container: Annotated[Container, Depends(get_container)]
) -> ReadinessResponse:
    """The database answers and its schema is at this build's migration head."""
    r = await check_readiness(container.health, container.expected_revision)
    if not r.ready:
        response.status_code = 503
    return ReadinessResponse(
        status="ready" if r.ready else "not_ready",
        database=r.database,
        migrations_current=r.migrations_current,
        migration_revision=r.migration_revision,
        expected_revision=r.expected_revision,
    )
