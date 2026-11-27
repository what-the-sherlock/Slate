"""Integrations (ADR-0032): the catalogue, the generic OAuth connect flow, disconnect.

Codes and state travel in request bodies, never in API URLs, so they stay out of logs:
the provider redirects to the *web* callback page, which strips them from the address
bar and POSTs them here. Changing routes need CSRF and a verified email.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status

from api.deps import Container, get_container
from api.schemas.integrations import (
    AuthorizeOut,
    CallbackBody,
    ConnectionOut,
    IntegrationOut,
    SyncConnectionOut,
)
from api.security import CurrentIdentity, MutatingIdentity
from core.domain.errors import UpstreamUnavailable
from core.ports.integrations import Connection
from core.services.integrations import IntegrationService, IntegrationView

router = APIRouter(tags=["integrations"])


def _service(container: Annotated[Container, Depends(get_container)]) -> IntegrationService:
    if container.integrations is None:
        raise UpstreamUnavailable("Integrations aren't configured on this server.")
    return container.integrations


Integrations = Annotated[IntegrationService, Depends(_service)]


def _connection(c: Connection, source_id: UUID | None = None) -> ConnectionOut:
    return ConnectionOut(
        id=c.id,
        source_id=source_id,
        account_label=c.account_label,
        status=c.status.value,
        last_error=c.last_error,
        connected_at=c.created_at,
    )


def _integration(v: IntegrationView) -> IntegrationOut:
    s = v.spec
    return IntegrationOut(
        id=s.id,
        name=s.name,
        tagline=s.tagline,
        auth=s.auth.value,
        mode=s.mode.value,
        state=v.state.value,
        connect_path=s.connect_path,
        notice=s.notice,
        connections=[_connection(c, v.source_ids.get(c.id)) for c in v.connections],
        sources=v.sources,
        warnings=list(v.warnings),
    )


@router.get(
    "/integrations",
    summary="Every integration and your connection state for each",
    response_model=list[IntegrationOut],
)
async def catalog(identity: CurrentIdentity, service: Integrations) -> list[IntegrationOut]:
    return [_integration(v) for v in await service.catalog(identity)]


@router.post(
    "/integrations/{provider_id}/authorize",
    summary="Start connecting: the provider's sign-in URL",
    response_model=AuthorizeOut,
)
async def authorize(
    provider_id: str, identity: MutatingIdentity, service: Integrations
) -> AuthorizeOut:
    return AuthorizeOut(authorize_url=await service.start(identity, provider_id))


@router.post(
    "/integrations/{provider_id}/callback",
    summary="Finish connecting with the code the provider sent back",
    response_model=ConnectionOut,
)
async def callback(
    provider_id: str, body: CallbackBody, identity: MutatingIdentity, service: Integrations
) -> ConnectionOut:
    return _connection(await service.complete(identity, provider_id, body.code, body.state))


@router.delete(
    "/integrations/connections/{connection_id}",
    summary="Disconnect: revoke access and delete the stored tokens",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def disconnect(
    connection_id: UUID, identity: MutatingIdentity, service: Integrations
) -> None:
    await service.disconnect(identity, connection_id)


@router.post(
    "/integrations/connections/{connection_id}/sync",
    summary="Sync a connected account now (also finishes a setup that failed after sign-in)",
    response_model=SyncConnectionOut,
)
async def sync_connection(
    connection_id: UUID,
    identity: MutatingIdentity,
    container: Annotated[Container, Depends(get_container)],
) -> SyncConnectionOut:
    if container.connected is None:
        raise UpstreamUnavailable("Syncing isn't configured on this server.")
    source_id = await container.connected.sync_connection(identity, connection_id)
    return SyncConnectionOut(source_id=source_id)
