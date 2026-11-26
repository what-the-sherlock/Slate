from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, SecretStr


class MailProviderOut(BaseModel):
    id: Literal["gmail", "yahoo", "icloud", "fastmail"]
    name: str
    app_password_url: str = Field(description="Where the user creates an app password")
    domains: list[str] = Field(description="Address domains that suggest this provider")


class MailStatusOut(BaseModel):
    available: bool = Field(description="False until CREDENTIALS_FERNET_KEY is configured")
    providers: list[MailProviderOut]


class MailConnectBody(BaseModel):
    provider: Literal["gmail", "yahoo", "icloud", "fastmail"]
    address: str = Field(min_length=3, max_length=320)
    # SecretStr: never printed by repr/logging; lengths are checked by the service.
    app_password: SecretStr = Field(max_length=500)


class MailConnectedOut(BaseModel):
    source_id: UUID
    name: str
    job_id: UUID
