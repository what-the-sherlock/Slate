"""Discord sources (ADR-0027): add Slate's bot to a server, pick channels, sync them.

Connect flow (no secret is stored, nothing is trusted from the browser):
1. ``install_link`` signs a 15-minute ``state`` bound to this user and workspace.
2. Discord's "add bot" page redirects back with ``code``, ``guild_id`` and ``state``.
   ``complete_install`` checks the state, then exchanges the code: Discord's response names
   the server the bot was just added to (only someone with Manage Server can do that), and
   the user's own token reads *their* roles there before it is revoked.
3. From those roles it computes which channels *this user* can read, and returns a signed
   30-minute grant listing them. ``channels`` and ``connect`` accept only that grant, so a
   user can never index a channel they can't see themselves (owner decision: private
   channels are fine, but only your own).

One source per server, readable only by the person who connected it and private data
(ADR-0026). Connecting more channels of the same server later adds them to that source.

Sync (``DiscordSync``, a worker/inline job handler) is in ``discord_sync.py``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import cast
from uuid import UUID

import structlog

from core.domain.auth import Identity, Role
from core.domain.data_class import DataClass
from core.domain.discord import (
    GRANT_PURPOSE,
    INSTALL_STATE_PURPOSE,
    MAX_CHANNELS_PER_SOURCE,
    ChannelChoice,
    Guild,
    can_read,
    is_snowflake,
    picker_rows,
    source_name,
)
from core.domain.errors import (
    Conflict,
    NotFound,
    PermissionDenied,
    RateLimited,
    UpstreamUnavailable,
    ValidationFailed,
)
from core.domain.ids import JobId, SourceId
from core.domain.jobs import JobStatus
from core.domain.principals import user_principal
from core.domain.signed_tokens import sign, verify
from core.ports.discord import DiscordApi
from core.ports.jobs import JobQueue
from core.ports.rate_limit import Limit, RateLimiter
from core.ports.sources import SourceRecord, SourceStore
from core.ports.system import Clock

log = structlog.get_logger(__name__)

SYNC_DISCORD = "sync_discord"
CONNECTOR = "discord"
STATE_TTL = timedelta(minutes=15)
GRANT_TTL = timedelta(minutes=30)
MAX_GRANT_CHANNELS = 500
INSTALL_LIMIT = Limit(budget=10, window=timedelta(hours=1))
SYNC_NOW_LIMIT = Limit(budget=1, window=timedelta(minutes=5))
EXPIRED_LINK = "This Discord link has expired. Connect Discord again."
NOT_SET_UP = "Discord isn't set up on this server yet."


@dataclass(frozen=True, slots=True)
class ServerChannels:
    guild: Guild
    rows: list[ChannelChoice]


@dataclass(frozen=True, slots=True)
class DiscordConnected:
    source_id: SourceId
    job_id: JobId
    name: str
    channels: int


@dataclass(frozen=True, slots=True)
class _Grant:
    guild_id: str
    channels: frozenset[str]


class DiscordService:
    def __init__(
        self,
        discord: DiscordApi,
        sources: SourceStore,
        jobs: JobQueue,
        limiter: RateLimiter,
        clock: Clock,
        *,
        secret: bytes,
    ) -> None:
        self._dc = discord
        self._sources = sources
        self._jobs = jobs
        self._limiter = limiter
        self._clock = clock
        self._secret = secret

    @property
    def available(self) -> bool:
        return self._dc.configured

    def _require_admin(self, identity: Identity) -> None:
        if not self.available:
            raise UpstreamUnavailable(NOT_SET_UP)
        if not identity.can(Role.ADMIN):
            raise PermissionDenied("Only admins and the owner can connect Discord.")

    def install_link(self, identity: Identity) -> str:
        self._require_admin(identity)
        state = sign(
            {"u": str(identity.user_id), "w": str(identity.workspace_id)},
            secret=self._secret,
            purpose=INSTALL_STATE_PURPOSE,
            expires_at=self._clock.now() + STATE_TTL,
        )
        return self._dc.install_url(state)

    async def complete_install(
        self, identity: Identity, *, guild_id: str, code: str, state: str
    ) -> str:
        """Verify the redirect; return a grant naming the channels this user can read."""
        self._require_admin(identity)
        self._claims(state, INSTALL_STATE_PURPOSE, identity)
        if not is_snowflake(guild_id):
            raise ValidationFailed("That isn't a valid Discord server.")
        if not await self._limiter.try_spend(f"dc-install:{identity.user_id}", 1, INSTALL_LIMIT):
            raise RateLimited("Too many Discord connections. Try again later.", retry_after_s=3600)
        proof = await self._dc.verify_install(code, guild_id)
        if proof is None:
            raise PermissionDenied(
                "Discord didn't confirm you added Slate to this server. Connect Discord again."
            )
        guild = await self._dc.guild(guild_id)
        readable = sorted(
            c.id
            for c in await self._dc.channels(guild_id)
            if c.indexable and can_read(guild, proof.member, c)
        )[:MAX_GRANT_CHANNELS]
        log.info("discord.install_verified", readable=len(readable))
        return sign(
            {
                "u": str(identity.user_id),
                "w": str(identity.workspace_id),
                "g": guild_id,
                "c": readable,
            },
            secret=self._secret,
            purpose=GRANT_PURPOSE,
            expires_at=self._clock.now() + GRANT_TTL,
        )

    async def channels(self, identity: Identity, grant: str) -> ServerChannels:
        g = self._grant(grant, identity)
        guild = await self._dc.guild(g.guild_id)
        bot = await self._dc.bot_member(g.guild_id)
        existing = await self._existing(identity, g.guild_id)
        rows = picker_rows(
            guild,
            await self._dc.channels(g.guild_id),
            user_readable=g.channels,
            bot=bot,
            connected=set(_channel_ids(existing)) if existing else set(),
        )
        return ServerChannels(guild, rows)

    async def connect(
        self, identity: Identity, grant: str, channel_ids: Sequence[str]
    ) -> DiscordConnected:
        g = self._grant(grant, identity)
        wanted = list(dict.fromkeys(channel_ids))
        if not wanted:
            raise ValidationFailed("Choose at least one channel.")
        if not all(is_snowflake(c) for c in wanted):
            raise ValidationFailed("That isn't a valid Discord channel.")
        not_yours = [c for c in wanted if c not in g.channels]
        if not_yours:
            raise PermissionDenied("You can only connect channels you can read yourself.")
        guild = await self._dc.guild(g.guild_id)
        bot = await self._dc.bot_member(g.guild_id)
        rows = {
            r.channel.id: r
            for r in picker_rows(
                guild, await self._dc.channels(g.guild_id), user_readable=g.channels, bot=bot
            )
        }
        missing = [c for c in wanted if c not in rows]
        if missing:
            raise NotFound("One of those channels no longer exists. Reload the list.")
        blocked = [rows[c].channel.name for c in wanted if not rows[c].bot_can_read]
        if blocked:
            raise PermissionDenied(
                "Slate's bot can't read #" + ", #".join(blocked) + ". Give its role access first."
            )
        existing = await self._existing(identity, g.guild_id)
        if existing is not None and existing.created_by != identity.user_id:
            # Its ACL is the other person's: adding channels you picked would show them
            # to that person (and hide them from you).
            raise Conflict("Someone else in this workspace already connected this server.")
        before: list[str] = _channel_ids(existing) if existing else []
        merged: list[str] = list(dict.fromkeys([*before, *wanted]))
        if len(merged) > MAX_CHANNELS_PER_SOURCE:
            raise ValidationFailed(
                f"Connect at most {MAX_CHANNELS_PER_SOURCE} channels per server."
            )
        config: dict[str, object] = {
            "guild_id": guild.id,
            "guild_name": guild.name,
            "channels": [
                {"id": c, "name": rows[c].channel.name if c in rows else _old_name(existing, c)}
                for c in merged
            ],
        }
        ws = identity.workspace_id
        if existing is not None:
            await self._sources.update_config(ws, existing.id, config)
            sid, name = existing.id, existing.name
            key = f"{SYNC_DISCORD}:{sid}:added:{len(merged)}:{int(self._clock.now().timestamp())}"
        else:
            taken = {s.name for s in await self._sources.sources_by_connector(ws, CONNECTOR)}
            name = source_name(guild.name, guild.id, taken)
            try:
                # Readable only by whoever connected it, and private data (ADR-0026).
                sid = await self._sources.create_source(
                    ws,
                    connector=CONNECTOR,
                    name=name,
                    config=config,
                    visibility=[user_principal(identity.user_id)],
                    data_class=DataClass.PRIVATE,
                    created_by=identity.user_id,
                )
            except Conflict:
                # Another source in this workspace has that name: disambiguate once more.
                name = source_name(guild.name, guild.id, taken | {name})
                sid = await self._sources.create_source(
                    ws,
                    connector=CONNECTOR,
                    name=name,
                    config=config,
                    visibility=[user_principal(identity.user_id)],
                    data_class=DataClass.PRIVATE,
                    created_by=identity.user_id,
                )
            key = f"{SYNC_DISCORD}:{sid}:initial"
        job = await self._jobs.enqueue(ws, SYNC_DISCORD, {"source_id": str(sid)}, key)
        log.info("discord.connected", channels=len(merged), added=existing is not None)
        return DiscordConnected(sid, job, name, len(merged))

    async def sync_now(self, identity: Identity, source_id: SourceId) -> JobId:
        if not identity.can(Role.MEMBER):
            raise PermissionDenied("Your role can't sync sources.")
        source = await self._source(identity, source_id)
        initial = await self._jobs.find_by_key(f"{SYNC_DISCORD}:{source.id}:initial")
        if initial is not None and initial.status in (JobStatus.QUEUED, JobStatus.FAILED):
            return initial.id
        if not await self._limiter.try_spend(f"dc-sync:{source.id}", 1, SYNC_NOW_LIMIT):
            raise RateLimited(
                "This server was synced a moment ago. Try again in a few minutes.",
                retry_after_s=300,
            )
        bucket = int(self._clock.now().timestamp()) // 300
        return await self._jobs.enqueue(
            identity.workspace_id,
            SYNC_DISCORD,
            {"source_id": str(source.id)},
            f"{SYNC_DISCORD}:{source.id}:manual:{bucket}",
        )

    async def disconnect(self, identity: Identity, source_id: SourceId) -> None:
        """Delete the source and what it indexed. The bot leaves the server too, unless
        another Slate source (any account) still uses it: nothing is left behind."""
        source = await self._source(identity, source_id)
        # Whoever connected it can always delete their own data, whatever their role.
        if not identity.can(Role.ADMIN) and source.created_by != identity.user_id:
            raise PermissionDenied("Only admins, the owner and its connector can disconnect it.")
        guild_id = str(source.config.get("guild_id", ""))
        if not await self._sources.delete_source(identity.workspace_id, source.id):
            raise NotFound("This source doesn't exist.")
        if is_snowflake(guild_id) and not await self._sources.config_in_use(
            CONNECTOR, "guild_id", guild_id
        ):
            await self._dc.leave_guild(guild_id)
        log.info("discord.disconnected")

    async def enqueue_due(self, limit: int = 50) -> int:
        """Hourly worker pass: one sync job per due server per hour (idempotent key)."""
        hour = int(self._clock.now().timestamp()) // 3600
        due = await self._sources.due_sources(CONNECTOR, limit)
        for ws, sid in due:
            await self._jobs.enqueue(
                ws, SYNC_DISCORD, {"source_id": str(sid)}, f"{SYNC_DISCORD}:{sid}:{hour}"
            )
        return len(due)

    # ------------------------------------------------------------ helpers

    async def _existing(self, identity: Identity, guild_id: str) -> SourceRecord | None:
        for s in await self._sources.sources_by_connector(identity.workspace_id, CONNECTOR):
            if str(s.config.get("guild_id")) == guild_id:
                return s
        return None

    async def _source(self, identity: Identity, source_id: SourceId) -> SourceRecord:
        source = await self._sources.get_source(identity.workspace_id, source_id)
        if source is None or source.connector != CONNECTOR:
            raise NotFound("This source doesn't exist.")
        return source

    def _claims(self, token: str, purpose: str, identity: Identity) -> dict[str, object]:
        try:
            claims = verify(token, secret=self._secret, purpose=purpose, now=self._clock.now())
        except ValidationFailed:
            raise ValidationFailed(EXPIRED_LINK) from None
        if claims.get("u") != str(identity.user_id) or claims.get("w") != str(
            identity.workspace_id
        ):
            raise PermissionDenied(
                "This Discord link was started by another account. Connect Discord again."
            )
        return claims

    def _grant(self, grant: str, identity: Identity) -> _Grant:
        if not self.available:
            raise UpstreamUnavailable(NOT_SET_UP)
        claims = self._claims(grant, GRANT_PURPOSE, identity)
        guild, channels = claims.get("g"), claims.get("c")
        if not is_snowflake(guild) or not isinstance(channels, list):
            raise ValidationFailed(EXPIRED_LINK)
        ids = frozenset(str(c) for c in cast(list[object], channels) if is_snowflake(c))
        return _Grant(str(guild), ids)


def channel_configs(source: SourceRecord) -> list[tuple[str, str]]:
    """(id, name) of the channels a source syncs, validated (config is stored JSON)."""
    raw = source.config.get("channels")
    out: list[tuple[str, str]] = []
    for item in cast(list[object], raw) if isinstance(raw, list) else []:
        if isinstance(item, dict):
            entry = cast(dict[str, object], item)
            cid = entry.get("id")
            if is_snowflake(cid):
                out.append((str(cid), str(entry.get("name") or "channel")))
    return out[:MAX_CHANNELS_PER_SOURCE]


def _channel_ids(source: SourceRecord | None) -> list[str]:
    return [cid for cid, _ in channel_configs(source)] if source else []


def _old_name(source: SourceRecord | None, channel_id: str) -> str:
    for cid, name in channel_configs(source) if source else []:
        if cid == channel_id:
            return name
    return "channel"


def parse_source_id(raw: object) -> SourceId:
    try:
        return SourceId(UUID(str(raw)))
    except ValueError:
        return SourceId(UUID(int=0))  # matches no source: the job is skipped
