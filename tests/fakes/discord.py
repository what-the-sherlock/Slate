"""An in-memory Discord: servers with roles, channels, messages and threads; one bot."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from datetime import datetime

from core.domain.discord import Channel, Guild, Member, Message, Thread, snowflake_time
from core.domain.errors import NotFound, PermissionDenied, RateLimited, UnreadableFile
from core.ports.discord import InstallProof

BOT_ID = "900000000000000001"


@dataclass
class FakeServer:
    guild: Guild
    channels: dict[str, Channel] = field(default_factory=dict[str, Channel])
    members: dict[str, Member] = field(default_factory=dict[str, Member])
    messages: dict[str, list[Message]] = field(default_factory=dict[str, list[Message]])
    threads: dict[str, Thread] = field(default_factory=dict[str, Thread])
    # thread id -> when it was archived (None: still active)
    archived_at: dict[str, datetime | None] = field(default_factory=dict[str, datetime | None])
    bot_in: bool = True


class FakeDiscord:
    def __init__(self, *, configured: bool = True) -> None:
        self._configured = configured
        self.servers: dict[str, FakeServer] = {}
        self.codes: dict[str, tuple[str, str]] = {}  # code -> (guild id it added the bot to, user)
        self.used_codes: set[str] = set()
        self.revoked = 0
        self.left: list[str] = []
        self.rate_limited = False
        self.forbidden_channels: set[str] = set()
        self.message_calls = 0
        self.thread_fetches: list[str] = []
        self.cdn: dict[str, bytes] = {}  # signed URL -> file bytes (ADR-0043)
        self.downloads: list[str] = []

    @property
    def configured(self) -> bool:
        return self._configured

    def install_url(self, state: str) -> str:
        return f"https://discord.test/oauth2/authorize?state={state}"

    # ------------------------------------------------------------------ test setup

    def add_server(self, guild: Guild, *, bot_roles: tuple[str, ...] = ()) -> FakeServer:
        server = FakeServer(guild)
        server.members[BOT_ID] = Member(BOT_ID, bot_roles)
        self.servers[guild.id] = server
        return server

    def post(self, channel_id: str, *messages: Message) -> None:
        for server in self.servers.values():
            if channel_id in server.channels or channel_id in server.threads:
                server.messages.setdefault(channel_id, []).extend(messages)
                return
        raise KeyError(channel_id)

    # ------------------------------------------------------------------ API

    def _server(self, guild_id: str) -> FakeServer:
        server = self.servers.get(guild_id)
        if server is None or not server.bot_in:
            raise NotFound("That Discord server no longer exists, or Slate's bot was removed.")
        return server

    def _channel_server(self, channel_id: str) -> FakeServer:
        if self.rate_limited:
            raise RateLimited("Discord's rate limit is reached.", retry_after_s=30)
        if channel_id in self.forbidden_channels:
            raise PermissionDenied("Slate's bot can't read this anymore.")
        for server in self.servers.values():
            if server.bot_in and (channel_id in server.channels or channel_id in server.threads):
                return server
        raise NotFound("That Discord channel no longer exists, or Slate's bot was removed.")

    async def verify_install(self, code: str, guild_id: str) -> InstallProof | None:
        if code in self.used_codes:
            return None
        self.used_codes.add(code)
        added = self.codes.get(code)
        self.revoked += 1
        if added is None or added[0] != guild_id:
            return None
        member = self.servers[guild_id].members.get(added[1])
        return InstallProof(guild_id, member) if member else None

    async def guild(self, guild_id: str) -> Guild:
        return self._server(guild_id).guild

    async def channels(self, guild_id: str) -> list[Channel]:
        return list(self._server(guild_id).channels.values())

    async def bot_member(self, guild_id: str) -> Member:
        return self._server(guild_id).members[BOT_ID]

    async def messages_after(
        self, channel_id: str, after: int, limit: int
    ) -> AsyncIterator[Message]:
        server = self._channel_server(channel_id)
        self.message_calls += 1
        if channel_id in server.threads:
            self.thread_fetches.append(channel_id)
        msgs = sorted(server.messages.get(channel_id, []), key=lambda m: int(m.id))
        for m in [m for m in msgs if int(m.id) > after][:limit]:
            if self.rate_limited:
                raise RateLimited("Discord's rate limit is reached.", retry_after_s=30)
            yield m

    async def latest_messages(self, channel_id: str, since: int, limit: int) -> list[Message]:
        server = self._channel_server(channel_id)
        self.message_calls += 1
        msgs = sorted(server.messages.get(channel_id, []), key=lambda m: int(m.id))
        return [m for m in msgs if int(m.id) > since][-limit:]

    async def active_threads(self, guild_id: str) -> list[Thread]:
        server = self._server(guild_id)
        return [
            self._thread(server, t)
            for t in server.threads.values()
            if not server.archived_at.get(t.id)
        ]

    async def archived_threads(self, channel_id: str, since: datetime) -> AsyncIterator[Thread]:
        server = self._channel_server(channel_id)
        archived = [
            (at, t)
            for t in server.threads.values()
            if t.parent_id == channel_id
            and (at := server.archived_at.get(t.id)) is not None
            and not t.private
        ]
        for at, t in sorted(archived, key=lambda x: x[0], reverse=True):
            if at < since:
                return
            yield self._thread(server, t)

    @staticmethod
    def _thread(server: FakeServer, t: Thread) -> Thread:
        msgs = server.messages.get(t.id, [])
        last = max((m.id for m in msgs), key=int, default=None)
        return replace(
            t,
            last_message_id=last or t.last_message_id,
            archived=t.id in server.archived_at and server.archived_at[t.id] is not None,
        )

    async def download(self, url: str, max_bytes: int) -> bytes | None:
        self.downloads.append(url)
        data = self.cdn.get(url)
        if data is not None and len(data) > max_bytes:
            raise UnreadableFile("The file is too large.", reason="too_large")
        return data

    async def leave_guild(self, guild_id: str) -> None:
        self.left.append(guild_id)
        if guild_id in self.servers:
            self.servers[guild_id].bot_in = False


def at(snowflake: str) -> datetime:
    return snowflake_time(snowflake)
