"""Integrations API shapes (ADR-0032)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

IntegrationStateOut = Literal[
    "connected", "needs_reconnect", "not_connected", "not_set_up", "coming_soon"
]


class ConnectionOut(BaseModel):
    id: UUID
    account_label: str | None = Field(description="The signed-in account, e.g. an address")
    status: Literal["connected", "needs_reconnect", "error"]
    last_error: str | None = Field(description="The provider's error code, if the last use failed")
    connected_at: datetime
    source_id: UUID | None = Field(
        default=None, description="The source this connection syncs into (ADR-0033)"
    )


class IntegrationOut(BaseModel):
    id: str = Field(examples=["notion"])
    name: str
    tagline: str
    auth: Literal["oauth2", "github_app", "discord_bot", "app_password"]
    mode: Literal["indexed", "live"] = Field(
        description="indexed: synced and searchable with citations; live: looked up when asked"
    )
    state: IntegrationStateOut
    connect_path: str | None = Field(
        description="For the older install flows, the web page that connects it"
    )
    notice: str | None = Field(description="Shown before the user goes to the provider")
    connections: list[ConnectionOut]
    sources: int = Field(description="Sources created through the older install flows")
    warnings: list[str] = Field(
        default_factory=list[str], description="Things the user should know (e.g. duplicates)"
    )


class AuthorizeOut(BaseModel):
    authorize_url: str = Field(description="Send the browser here to sign in")


class CallbackBody(BaseModel):
    code: str = Field(min_length=1, max_length=2048)
    state: str = Field(min_length=1, max_length=2048)


class SyncConnectionOut(BaseModel):
    source_id: UUID = Field(description="The source being synced; open it to follow progress")
    status: Literal["done", "partial", "failed", "background"] = Field(
        description=(
            "done: synced now; partial: part synced, see reason; failed: it didn't work, see"
            " reason; background: the worker finishes it (big or busy)"
        )
    )
    reason: str | None = Field(default=None, description="Why it's partial or failed")
