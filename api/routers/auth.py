"""Auth routes (ADR-0021). Handlers only translate HTTP to AuthService and back."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status

from api.deps import Container, get_container
from api.schemas.auth import (
    AuthConfig,
    ForgotPasswordBody,
    LoginBody,
    MeResponse,
    MeUser,
    ResendBody,
    ResetPasswordBody,
    SignupBody,
    VerifyBody,
)
from api.security import (
    CurrentIdentity,
    SameSiteRequest,
    SessionMutatingIdentity,
    clear_session_cookies,
    client_ip,
    set_session_cookies,
)
from core.domain.auth import Identity
from core.services.auth import IssuedSession, SignupRequest

router = APIRouter(tags=["auth"])
ContainerDep = Annotated[Container, Depends(get_container)]


def _me(identity: Identity) -> MeResponse:
    return MeResponse(
        user=MeUser(id=identity.user_id, email=identity.email, name=identity.name),
        workspace_id=identity.workspace_id,
        role=identity.role.value,
        email_verified=identity.email_verified,
        is_platform_admin=identity.is_platform_admin,
    )


def _respond(container: Container, response: Response, issued: IssuedSession) -> MeResponse:
    set_session_cookies(container, response, issued)
    return _me(issued.identity)


@router.get("/auth/config", summary="What the sign-in screens need", response_model=AuthConfig)
async def auth_config(container: ContainerDep) -> AuthConfig:
    settings = container.settings
    return AuthConfig(
        signups_enabled=container.auth.signups_enabled,
        turnstile_site_key=settings.turnstile_site_key if settings.turnstile_configured else None,
    )


@router.post(
    "/auth/signup",
    summary="Create an account and its personal workspace, then sign in",
    response_model=MeResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[SameSiteRequest],
)
async def signup(
    body: SignupBody, request: Request, response: Response, container: ContainerDep
) -> MeResponse:
    issued = await container.auth.signup(
        SignupRequest(
            name=body.name,
            email=body.email,
            password=body.password,
            human_token=body.turnstile_token,
            setup_token=body.setup_token or "",
            trap=body.website,
        ),
        ip=client_ip(request, container),
        user_agent=request.headers.get("user-agent"),
    )
    return _respond(container, response, issued)


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
    return _respond(container, response, issued)


@router.post(
    "/auth/verify", summary="Verify your email with the emailed code", response_model=MeResponse
)
async def verify(
    body: VerifyBody, identity: SessionMutatingIdentity, request: Request, container: ContainerDep
) -> MeResponse:
    updated = await container.auth.verify_email(
        identity, body.code, ip=client_ip(request, container)
    )
    return _me(updated)


@router.post(
    "/auth/verify/resend",
    summary="Email a new verification code",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def resend(
    body: ResendBody, identity: SessionMutatingIdentity, request: Request, container: ContainerDep
) -> None:
    await container.auth.resend_verification(
        identity, human_token=body.turnstile_token, ip=client_ip(request, container)
    )


@router.post(
    "/auth/password/forgot",
    summary="Email a password reset code (same answer whether or not the account exists)",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[SameSiteRequest],
)
async def forgot_password(
    body: ForgotPasswordBody, request: Request, container: ContainerDep
) -> None:
    await container.auth.request_password_reset(
        body.email, human_token=body.turnstile_token, ip=client_ip(request, container)
    )


@router.post(
    "/auth/password/reset",
    summary="Set a new password with a reset code (signs out every session)",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[SameSiteRequest],
)
async def reset_password(
    body: ResetPasswordBody, request: Request, response: Response, container: ContainerDep
) -> None:
    await container.auth.reset_password(
        body.email, body.code, body.password, ip=client_ip(request, container)
    )
    clear_session_cookies(container, response)


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
