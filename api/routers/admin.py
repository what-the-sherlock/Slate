"""Platform admin console (ADR-0042).

Access is decided in AdminService, which answers 404 to everyone but the verified
OWNER_EMAIL account, so these routes look absent to anyone else. Reads need a session;
the one change (disable/enable) also needs CSRF and a verified email (MutatingIdentity).
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from api.deps import Container, get_container
from api.schemas.admin import (
    AdminUserOut,
    AuthEventOut,
    DeleteUserBody,
    EventKind,
    EventPageOut,
    OverviewOut,
    SeriesOut,
    SeriesPointOut,
    UserPageOut,
    UserStatusBody,
)
from api.security import CurrentIdentity, MutatingIdentity
from core.domain.admin import AdminUser, AuthEventKind, Bucket, SeriesPoint, UserSort, UserStatus
from core.domain.auth import Identity
from core.domain.ids import UserId
from core.services.admin import RANGES, AdminService

router = APIRouter(prefix="/admin", tags=["admin"])


def _service(container: Annotated[Container, Depends(get_container)]) -> AdminService:
    return container.admin_service


Admin = Annotated[AdminService, Depends(_service)]


def _user(u: AdminUser, identity: Identity) -> AdminUserOut:
    return AdminUserOut(
        id=u.id, email=u.email, name=u.name, joined_at=u.joined_at,
        email_verified=u.email_verified, disabled=u.disabled,
        last_active_at=u.last_active_at, workspaces=u.workspaces,
        you=u.id == identity.user_id,
    )  # fmt: skip


def _points(points: list[SeriesPoint]) -> list[SeriesPointOut]:
    return [SeriesPointOut(start=p.start, count=p.count) for p in points]


@router.get("/overview", summary="Account totals and activity", response_model=OverviewOut)
async def overview(identity: CurrentIdentity, svc: Admin) -> OverviewOut:
    o = await svc.overview(identity)
    return OverviewOut(**{f: getattr(o, f) for f in OverviewOut.model_fields})


@router.get("/signups", summary="Sign-ups over time", response_model=SeriesOut)
async def signups(
    identity: CurrentIdentity,
    svc: Admin,
    days: Annotated[int, Query(description="30, 90 or 365")] = 30,
    bucket: Annotated[Literal["day", "week", "month"] | None, Query()] = None,
) -> SeriesOut:
    chosen = Bucket(bucket) if bucket else None
    points = await svc.signups(identity, days=days, bucket=chosen)
    return SeriesOut(bucket=(chosen or RANGES[days]).value, points=_points(points))


@router.get("/failed-logins", summary="Failed sign-ins per day", response_model=SeriesOut)
async def failed_logins(
    identity: CurrentIdentity,
    svc: Admin,
    days: Annotated[int, Query(ge=1, le=90)] = 30,
) -> SeriesOut:
    return SeriesOut(bucket="day", points=_points(await svc.failed_logins(identity, days=days)))


@router.get("/users", summary="Accounts on the platform", response_model=UserPageOut)
async def users(
    identity: CurrentIdentity,
    svc: Admin,
    q: Annotated[str, Query(max_length=100)] = "",
    status: Annotated[
        Literal["all", "verified", "unverified", "disabled", "active", "inactive"], Query()
    ] = "all",
    sort: Annotated[Literal["joined", "last_active", "email"], Query()] = "joined",
    order: Annotated[Literal["asc", "desc"], Query()] = "desc",
    cursor: Annotated[str | None, Query(max_length=12)] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 25,
) -> UserPageOut:
    page = await svc.users(
        identity, query=q, status=UserStatus(status), sort=UserSort(sort),
        descending=order == "desc", cursor=cursor, limit=limit,
    )  # fmt: skip
    return UserPageOut(
        items=[_user(u, identity) for u in page.items],
        next_cursor=page.next_cursor,
        total=page.total or 0,
    )


@router.get("/events", summary="Recent security events", response_model=EventPageOut)
async def events(
    identity: CurrentIdentity,
    svc: Admin,
    kind: Annotated[EventKind | None, Query()] = None,
    cursor: Annotated[str | None, Query(max_length=12)] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 50,
) -> EventPageOut:
    page = await svc.events(
        identity, kind=AuthEventKind(kind) if kind else None, cursor=cursor, limit=limit
    )
    return EventPageOut(
        items=[
            AuthEventOut(
                id=e.id, kind=e.kind.value, at=e.at, user_id=e.user_id, user_email=e.user_email
            )
            for e in page.items
        ],
        next_cursor=page.next_cursor,
    )


@router.patch(
    "/users/{user_id}", summary="Disable or re-enable an account", response_model=AdminUserOut
)
async def set_status(
    user_id: UUID, body: UserStatusBody, identity: MutatingIdentity, svc: Admin
) -> AdminUserOut:
    updated = await svc.set_disabled(identity, UserId(user_id), disabled=body.disabled)
    return _user(updated, identity)


@router.post(
    "/users/{user_id}/delete",
    summary="Permanently delete an account and everything it owns",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_user(
    user_id: UUID, body: DeleteUserBody, identity: MutatingIdentity, svc: Admin
) -> None:
    # POST with a body (not DELETE): the typed confirmation travels in the request body.
    await svc.delete_user(identity, UserId(user_id), confirm_email=body.confirm_email)
