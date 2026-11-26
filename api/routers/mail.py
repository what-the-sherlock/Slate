"""Email sources (ADR-0028). Handlers translate HTTP to MailService and back.

The app password travels once, in the body of a CSRF-protected POST, and is never logged
or echoed (422 responses carry field names, not values). Sync now and disconnect are the
generic ``/sources/{id}`` routes; the .mbox import is a normal upload (``/uploads``).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status

from api.deps import Container, get_container
from api.schemas.mail import MailConnectBody, MailConnectedOut, MailProviderOut, MailStatusOut
from api.security import CurrentIdentity, MutatingIdentity
from core.domain.errors import UpstreamUnavailable
from core.domain.mail import PROVIDERS, Provider
from core.services.mail import NOT_SET_UP, MailService

router = APIRouter(tags=["email"])
ContainerDep = Annotated[Container, Depends(get_container)]


def _mail(container: ContainerDep) -> MailService:
    if container.mail is None or not container.mail.available:
        raise UpstreamUnavailable(NOT_SET_UP)
    return container.mail


Mail = Annotated[MailService, Depends(_mail)]


@router.get(
    "/sources/email/status",
    summary="Whether a mailbox can be connected, and which providers",
    response_model=MailStatusOut,
)
async def email_status(identity: CurrentIdentity, container: ContainerDep) -> MailStatusOut:
    return MailStatusOut(
        available=container.mail is not None and container.mail.available,
        providers=[
            MailProviderOut(
                id=p.value,
                name=info.name,
                app_password_url=info.app_password_url,
                domains=sorted(info.domains),
            )
            for p, info in PROVIDERS.items()
        ],
    )


@router.post(
    "/sources/email/connect",
    summary="Connect a mailbox over IMAP with an app password (read-only)",
    response_model=MailConnectedOut,
    status_code=status.HTTP_201_CREATED,
)
async def connect(
    body: MailConnectBody, identity: MutatingIdentity, mail: Mail
) -> MailConnectedOut:
    done = await mail.connect(
        identity,
        provider=Provider(body.provider),
        address=body.address,
        password=body.app_password.get_secret_value(),
    )
    return MailConnectedOut(source_id=done.source_id, name=done.name, job_id=done.job_id)
