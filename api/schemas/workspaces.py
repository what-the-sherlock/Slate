from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

InvitableRole = Literal["admin", "member", "viewer"]
AnyRole = Literal["owner", "admin", "member", "viewer"]


class WorkspaceOut(BaseModel):
    id: UUID
    name: str
    role: AnyRole
    members: int
    active: bool


class SwitchBody(BaseModel):
    workspace_id: UUID


class MemberOut(BaseModel):
    user_id: UUID
    email: str
    name: str
    role: AnyRole
    joined_at: datetime
    you: bool


class InviteBody(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    role: InvitableRole = "member"


class InviteOut(BaseModel):
    id: UUID
    email: str
    role: AnyRole
    created_at: datetime
    expires_at: datetime


class InviteCreatedOut(BaseModel):
    id: UUID
    link: str = Field(description="Share this with the invitee if it wasn't emailed")
    emailed: bool


class AcceptBody(BaseModel):
    token: str = Field(min_length=20, max_length=100)


class AcceptedOut(BaseModel):
    workspace_id: UUID


class RoleBody(BaseModel):
    role: InvitableRole


class RemovedOut(BaseModel):
    sources_deleted: int


class ShareBody(BaseModel):
    shared: bool = Field(description="True: every member of this workspace can search it")
