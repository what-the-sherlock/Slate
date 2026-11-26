"""Discord sources (ADR-0027). Handlers translate HTTP to DiscordService and back.

Every route that changes something needs CSRF and a verified email (MutatingIdentity).
Grants and OAuth codes travel in request bodies, never in URLs, so they stay out of logs.
Sync now and disconnect are the generic ``/sources/{id}`` routes (sources.py).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status

from api.deps import Container, get_container
from api.schemas.discord import (
    DiscordChannelOut,
    DiscordConnectBody,
    DiscordConnectedOut,
    DiscordGrantBody,
    DiscordGrantOut,
    DiscordInstallBody,
    DiscordInstallLinkOut,
    DiscordServerOut,
    DiscordStatusOut,
)
from api.security import CurrentIdentity, MutatingIdentity
from core.domain.discord import ChannelChoice, ChannelType
from core.domain.errors import UpstreamUnavailable
from core.services.discord import NOT_SET_UP, DiscordService

router = APIRouter(tags=["discord"])
ContainerDep = Annotated[Container, Depends(get_container)]

_KINDS = {
    ChannelType.TEXT: "text",
    ChannelType.ANNOUNCEMENT: "announcement",
    ChannelType.FORUM: "forum",
    ChannelType.MEDIA: "media",
}


def _discord(container: ContainerDep) -> DiscordService:
    if container.discord is None:
        raise UpstreamUnavailable(NOT_SET_UP)
    return container.discord


Discord = Annotated[DiscordService, Depends(_discord)]


def _reason(row: ChannelChoice) -> str | None:
    if not row.user_can_read:
        return "You can't read this channel"
    if not row.bot_can_read:
        return "Slate's bot can't read this channel"
    return None


@router.get(
    "/sources/discord/status",
    summary="Whether Discord can be connected",
    response_model=DiscordStatusOut,
)
async def discord_status(identity: CurrentIdentity, container: ContainerDep) -> DiscordStatusOut:
    return DiscordStatusOut(available=container.discord is not None and container.discord.available)


@router.post(
    "/sources/discord/install",
    summary="Start adding Slate's bot to a Discord server",
    response_model=DiscordInstallLinkOut,
)
async def install(identity: MutatingIdentity, discord: Discord) -> DiscordInstallLinkOut:
    return DiscordInstallLinkOut(url=discord.install_link(identity))


@router.post(
    "/sources/discord/installations",
    summary="Finish adding the bot: verify it, get a grant for picking channels",
    response_model=DiscordGrantOut,
)
async def installation(
    body: DiscordInstallBody, identity: MutatingIdentity, discord: Discord
) -> DiscordGrantOut:
    grant = await discord.complete_install(
        identity, guild_id=body.guild_id, code=body.code, state=body.state
    )
    return DiscordGrantOut(grant=grant)


@router.post(
    "/sources/discord/channels",
    summary="The server's channels, and which ones you can connect",
    response_model=DiscordServerOut,
)
async def channels(
    body: DiscordGrantBody, identity: MutatingIdentity, discord: Discord
) -> DiscordServerOut:
    server = await discord.channels(identity, body.grant)
    g = server.guild
    icon = f"https://cdn.discordapp.com/icons/{g.id}/{g.icon}.png?size=64" if g.icon else None
    return DiscordServerOut(
        id=g.id,
        name=g.name,
        icon_url=icon,
        channels=[
            DiscordChannelOut(
                id=r.channel.id,
                name=r.channel.name,
                kind=_KINDS.get(ChannelType(r.channel.type), "text"),
                category=r.category,
                private=r.private,
                selectable=r.selectable,
                reason=_reason(r),
                connected=r.connected,
            )
            for r in server.rows
        ],
    )


@router.post(
    "/sources/discord/connect",
    summary="Connect channels of a server as one source (it starts syncing)",
    response_model=DiscordConnectedOut,
    status_code=status.HTTP_201_CREATED,
)
async def connect(
    body: DiscordConnectBody, identity: MutatingIdentity, discord: Discord
) -> DiscordConnectedOut:
    done = await discord.connect(identity, body.grant, body.channels)
    return DiscordConnectedOut(
        source_id=done.source_id, name=done.name, job_id=done.job_id, channels=done.channels
    )
