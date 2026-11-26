from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field

from core.domain.discord import MAX_CHANNELS_PER_SOURCE

# A grant lists every channel the user can read (up to 500 ids), so it's longer than
# GitHub's; still a bounded, signed string that travels only in request bodies.
GRANT_MAX = 20_000


class DiscordStatusOut(BaseModel):
    available: bool = Field(description="False until the server's Discord bot is configured")


class DiscordInstallLinkOut(BaseModel):
    url: str


class DiscordInstallBody(BaseModel):
    guild_id: str = Field(pattern=r"^[0-9]{15,21}$")
    code: str = Field(min_length=1, max_length=200)
    state: str = Field(min_length=1, max_length=2000)


class DiscordGrantOut(BaseModel):
    grant: str = Field(description="Short-lived proof of the server and the channels you can read")


class DiscordGrantBody(BaseModel):
    grant: str = Field(min_length=1, max_length=GRANT_MAX)


class DiscordChannelOut(BaseModel):
    id: str
    name: str
    kind: str = Field(description="text | announcement | forum | media")
    category: str | None
    private: bool = Field(description="Hidden from @everyone: only some roles can see it")
    selectable: bool
    reason: str | None = Field(description="Why it can't be picked, when it can't")
    connected: bool


class DiscordServerOut(BaseModel):
    id: str
    name: str
    icon_url: str | None
    channels: list[DiscordChannelOut]


class DiscordConnectBody(BaseModel):
    grant: str = Field(min_length=1, max_length=GRANT_MAX)
    channels: list[str] = Field(min_length=1, max_length=MAX_CHANNELS_PER_SOURCE)


class DiscordConnectedOut(BaseModel):
    source_id: UUID
    name: str
    job_id: UUID
    channels: int
