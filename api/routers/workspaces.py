"""Workspaces, members, invitations and sharing (ADR-0030).

Reads need a session; every change needs CSRF and a verified email (MutatingIdentity).
Invitation tokens travel only in request bodies (the email link keeps them in the URL
fragment), never in paths or query strings.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status

from api.deps import Container, get_container
from api.schemas.workspaces import (
    AcceptBody,
    AcceptedOut,
    InviteBody,
    InviteCreatedOut,
    InviteOut,
    MemberOut,
    RemovedOut,
    RoleBody,
    ShareBody,
    SwitchBody,
    WorkspaceOut,
)
from api.security import CurrentIdentity, MutatingIdentity
from core.domain.auth import Role
from core.domain.ids import SourceId, UserId, WorkspaceId
from core.services.workspaces import WorkspaceService

router = APIRouter(tags=["workspaces"])


def _service(container: Annotated[Container, Depends(get_container)]) -> WorkspaceService:
    return container.workspaces_service


Workspaces = Annotated[WorkspaceService, Depends(_service)]


@router.get("/workspaces", summary="Workspaces you belong to", response_model=list[WorkspaceOut])
async def list_workspaces(identity: CurrentIdentity, svc: Workspaces) -> list[WorkspaceOut]:
    return [
        WorkspaceOut(
            id=w.workspace_id,
            name=w.name,
            role=w.role.value,
            members=w.members,
            active=w.workspace_id == identity.workspace_id,
        )
        for w in await svc.workspaces(identity)
    ]


@router.post("/workspaces/switch", summary="Switch the active workspace", status_code=204)
async def switch(body: SwitchBody, identity: MutatingIdentity, svc: Workspaces) -> None:
    await svc.switch(identity, WorkspaceId(body.workspace_id))


@router.get(
    "/workspaces/members", summary="Members of the active workspace", response_model=list[MemberOut]
)
async def members(identity: CurrentIdentity, svc: Workspaces) -> list[MemberOut]:
    return [
        MemberOut(
            user_id=m.user_id,
            email=m.email,
            name=m.name,
            role=m.role.value,
            joined_at=m.joined_at,
            you=m.user_id == identity.user_id,
        )
        for m in await svc.members(identity)
    ]


@router.patch(
    "/workspaces/members/{user_id}", summary="Change a member's role (owner only)", status_code=204
)
async def change_role(
    user_id: UUID, body: RoleBody, identity: MutatingIdentity, svc: Workspaces
) -> None:
    await svc.set_role(identity, UserId(user_id), Role(body.role))


@router.delete(
    "/workspaces/members/{user_id}",
    summary="Remove a member, or leave (your own id); deletes what they connected here",
    response_model=RemovedOut,
)
async def remove_member(user_id: UUID, identity: MutatingIdentity, svc: Workspaces) -> RemovedOut:
    return RemovedOut(sources_deleted=await svc.remove(identity, UserId(user_id)))


@router.get(
    "/workspaces/invites", summary="Open invitations (admins)", response_model=list[InviteOut]
)
async def invites(identity: CurrentIdentity, svc: Workspaces) -> list[InviteOut]:
    return [
        InviteOut(
            id=i.id,
            email=i.email,
            role=i.role.value,
            created_at=i.created_at,
            expires_at=i.expires_at,
        )
        for i in await svc.invites(identity)
    ]


@router.post(
    "/workspaces/invites",
    summary="Invite someone by email",
    response_model=InviteCreatedOut,
    status_code=status.HTTP_201_CREATED,
)
async def invite(body: InviteBody, identity: MutatingIdentity, svc: Workspaces) -> InviteCreatedOut:
    done = await svc.invite(identity, email=body.email, role=Role(body.role))
    return InviteCreatedOut(id=done.id, link=done.link, emailed=done.emailed)


@router.delete("/workspaces/invites/{invite_id}", summary="Revoke an invitation", status_code=204)
async def revoke(invite_id: UUID, identity: MutatingIdentity, svc: Workspaces) -> None:
    await svc.revoke(identity, invite_id)


@router.post(
    "/invites/accept",
    summary="Accept an invitation sent to your address, and switch to that workspace",
    response_model=AcceptedOut,
)
async def accept(body: AcceptBody, identity: MutatingIdentity, svc: Workspaces) -> AcceptedOut:
    return AcceptedOut(workspace_id=await svc.accept(identity, body.token))


@router.put(
    "/sources/{source_id}/sharing",
    summary="Share a source you connected with this workspace, or make it private again",
    status_code=204,
)
async def share(
    source_id: UUID, body: ShareBody, identity: MutatingIdentity, svc: Workspaces
) -> None:
    await svc.share(identity, SourceId(source_id), shared=body.shared)
