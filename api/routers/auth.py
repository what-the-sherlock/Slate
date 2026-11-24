from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status

from api.deps import Container, get_container
from api.schemas.auth import LoginBody, MeResponse, MeUser, SetupBody, SetupStatus
from api.security import (
    CurrentIdentity,
    SameSiteRequest,
    SessionMutatingIdentity,
    clear_session_cookies,
    client_ip,
    set_session_cookies,
)
from core.domain.auth import Identity
from core.services.auth import IssuedSession, SetupRequest

router = APIRouter(tags=["auth"])
ContainerDep = Annotated[Container, Depends(get_container)]


def _me(identity: Identity) -> MeResponse:
    return MeResponse(
        user=MeUser(id=identity.user_id, email=identity.email, name=identity.name),
        workspace_id=identity.workspace_id,
        role=identity.role.value,
        is_guest=identity.is_guest,
    )


async def _respond(container: Container, response: Response, issued: IssuedSession) -> MeResponse:
    identity = await container.auth.authenticate(issued.session_token)
    assert identity is not None  # just issued
    set_session_cookies(container, response, issued)
    return _me(identity)


@router.get(
    "/auth/status", summary="Whether first-run setup has happened", response_model=SetupStatus
)
async def setup_status(container: ContainerDep) -> SetupStatus:
    return SetupStatus(
        set_up=await container.auth.is_set_up(),
        requires_setup_token=container.auth.setup_requires_token,
    )


@router.post(
    "/auth/setup",
    summary="Create the workspace and its owner (first run only)",
    response_model=MeResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[SameSiteRequest],
)
async def setup(
    body: SetupBody, request: Request, response: Response, container: ContainerDep
) -> MeResponse:
    issued = await container.auth.setup(
        SetupRequest(
            body.workspace_name, body.name, body.email, body.password, body.setup_token or ""
        ),
        ip=client_ip(request, container),
        user_agent=request.headers.get("user-agent"),
    )
    return await _respond(container, response, issued)


@router.post(
    "/auth/login",
    summary="Sign in with email and password",
    response_model=MeResponse,
    dependencies=[SameSiteRequest],
)
async def login(
    body: LoginBody, request: Request, response: Response, container: ContainerDep
) -> MeResponse:
    issued = await container.auth.login(
        body.email,
        body.password,
        ip=client_ip(request, container),
        user_agent=request.headers.get("user-agent"),
    )
    return await _respond(container, response, issued)


@router.post(
    "/auth/guest",
    summary="Start a read-only guest session",
    response_model=MeResponse,
    dependencies=[SameSiteRequest],
)
async def guest(request: Request, response: Response, container: ContainerDep) -> MeResponse:
    issued = await container.auth.login_as_guest(
        ip=client_ip(request, container), user_agent=request.headers.get("user-agent")
    )
    return await _respond(container, response, issued)


@router.post(
    "/auth/logout",
    summary="Sign out (ends this session)",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def logout(
    identity: SessionMutatingIdentity, response: Response, container: ContainerDep
) -> None:
    await container.auth.logout(identity)
    clear_session_cookies(container, response)


@router.get("/me", summary="The signed-in user", response_model=MeResponse)
async def me(identity: CurrentIdentity) -> MeResponse:
    return _me(identity)
