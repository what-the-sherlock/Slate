"""What Slate needs from Discord (ADR-0027). The adapter talks to Discord as one Slate bot.

Connectors only fetch (rule 2): nothing here parses, chunks, embeds or writes.
Errors: NotFound when the server or channel is gone; PermissionDenied when the bot lost
access; RateLimited (with retry_after_s) on Discord's limits; UpstreamUnavailable otherwise.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from core.domain.discord import Channel, Guild, Member, Message, Thread


@dataclass(frozen=True, slots=True)
class InstallProof:
    """Who added the bot, to which server, and their roles there."""

    guild_id: str
    member: Member


class DiscordApi(Protocol):
    @property
    def configured(self) -> bool:
        """False when the bot's settings are missing (the feature is then hidden)."""
        ...

    def install_url(self, state: str) -> str:
        """Discord's "add bot" page, asking for read-only permissions and the user's roles."""
        ...

    async def verify_install(self, code: str, guild_id: str) -> InstallProof | None:
        """Exchange the redirect's OAuth ``code``. Discord's token response names the
        server the bot was just added to (adding a bot needs Manage Server there); if that
        isn't ``guild_id``, return None. Read the user's own roles in it with their token,
        then revoke the token. None when anything doesn't check out.
        """
        ...

    async def guild(self, guild_id: str) -> Guild: ...

    async def channels(self, guild_id: str) -> list[Channel]: ...

    async def bot_member(self, guild_id: str) -> Member:
        """The bot's own membership (its roles decide what it can read)."""
        ...

    def messages_after(self, channel_id: str, after: int, limit: int) -> AsyncIterator[Message]:
        """Messages with id > ``after``, oldest first, at most ``limit``."""
        ...

    async def latest_messages(self, channel_id: str, since: int, limit: int) -> list[Message]:
        """The newest messages with id > ``since``, at most ``limit``, oldest first.
        (The first sync reads backwards so a busy channel keeps its *latest* history.)"""
        ...

    async def active_threads(self, guild_id: str) -> list[Thread]: ...

    def archived_threads(self, channel_id: str, since: datetime) -> AsyncIterator[Thread]:
        """Public archived threads, newest archive first, until one archived before
        ``since``."""
        ...

    async def leave_guild(self, guild_id: str) -> None:
        """Remove the bot from a server. Best effort: never raises."""
        ...
