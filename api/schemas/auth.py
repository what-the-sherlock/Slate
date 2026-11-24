from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class SetupStatus(BaseModel):
    set_up: bool = Field(description="False until the first owner has created the workspace.")
    requires_setup_token: bool = Field(description="Whether setup asks for the server's code.")


class SetupBody(BaseModel):
    workspace_name: str = Field(min_length=1, max_length=80, examples=["Acme"])
    name: str = Field(min_length=1, max_length=120, examples=["Ada Lovelace"])
    email: str = Field(min_length=3, max_length=254, examples=["ada@acme.dev"])
    password: str = Field(min_length=1, max_length=256)
    setup_token: str | None = Field(default=None, max_length=256)


class LoginBody(BaseModel):
    email: str = Field(min_length=3, max_length=254, examples=["ada@acme.dev"])
    password: str = Field(min_length=1, max_length=256)


class MeUser(BaseModel):
    id: UUID
    email: str
    name: str


class MeResponse(BaseModel):
    user: MeUser
    workspace_id: UUID
    role: Literal["owner", "admin", "member", "viewer"]
    is_guest: bool
