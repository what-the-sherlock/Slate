"""GitHub sources (ADR-0023). Handlers translate HTTP to GitHubService and back.

Every route that changes something needs CSRF and a verified email (MutatingIdentity).
Grants travel in request bodies, never in URLs, so they stay out of logs and history.
Sync now and disconnect are the generic ``/sources/{id}`` routes (sources.py).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status

from api.deps import Container, get_container
from api.schemas.github import (
    ConnectBody,
    ConnectedOut,
    GitHubStatusOut,
    GrantBody,
    GrantOut,
    InstallationBody,
    InstallLinkOut,
    RepoOut,
)
from api.security import CurrentIdentity, MutatingIdentity
from core.domain.errors import UpstreamUnavailable
from core.services.github import GitHubService

router = APIRouter(tags=["github"])
ContainerDep = Annotated[Container, Depends(get_container)]


def _github(container: ContainerDep) -> GitHubService:
    if container.github is None:
        raise UpstreamUnavailable("GitHub isn't set up on this server yet.")
    return container.github


GitHub = Annotated[GitHubService, Depends(_github)]


@router.get(
    "/sources/github/status",
    summary="Whether GitHub can be connected",
    response_model=GitHubStatusOut,
)
async def github_status(identity: CurrentIdentity, container: ContainerDep) -> GitHubStatusOut:
    return GitHubStatusOut(available=container.github is not None and container.github.available)


@router.post(
    "/sources/github/install",
    summary="Start installing the GitHub App",
    response_model=InstallLinkOut,
)
async def install(identity: MutatingIdentity, github: GitHub) -> InstallLinkOut:
    return InstallLinkOut(url=github.install_link(identity))


@router.post(
    "/sources/github/installations",
    summary="Finish the install: verify it's yours, get a grant for picking repos",
    response_model=GrantOut,
)
async def installation(
    body: InstallationBody, identity: MutatingIdentity, github: GitHub
) -> GrantOut:
    grant = await github.complete_install(
        identity, installation_id=body.installation_id, code=body.code, state=body.state
    )
    return GrantOut(grant=grant)


@router.post("/sources/github/repos", summary="Repos the app can see", response_model=list[RepoOut])
async def repos(body: GrantBody, identity: MutatingIdentity, github: GitHub) -> list[RepoOut]:
    return [
        RepoOut(
            id=c.repo.id,
            full_name=c.repo.full_name,
            private=c.repo.private,
            description=c.repo.description,
            html_url=c.repo.html_url,
            connected=c.connected,
        )
        for c in await github.repos(identity, body.grant)
    ]


@router.post(
    "/sources/github/connect",
    summary="Connect repos as sources (each starts syncing)",
    response_model=list[ConnectedOut],
    status_code=status.HTTP_201_CREATED,
)
async def connect(
    body: ConnectBody, identity: MutatingIdentity, github: GitHub
) -> list[ConnectedOut]:
    done = await github.connect(identity, body.grant, body.repos)
    return [ConnectedOut(source_id=c.source_id, name=c.name, job_id=c.job_id) for c in done]
