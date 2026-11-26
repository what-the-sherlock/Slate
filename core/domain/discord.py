"""Discord connector rules (ADR-0027). Pure: no I/O.

What Slate indexes from a server: the text, announcement, forum and media channels the
connecting user picked, as two kinds of document:

* a **thread** (including a forum post) is one document;
* other channel messages are grouped into **windows**: a window closes after a 30-minute
  silence or at 50 messages, so one document is roughly one conversation.

Access (owner decision, 2026-11-26): any channel the *connecting user* can read, private
ones included, and only if Slate's bot can read it too. Channel visibility is computed with
Discord's documented permission algorithm (``channel_permissions``). Private threads are
never indexed: they are member-only by design.

Sync reads REST only (serverless can't hold a Gateway connection): the first sync reads the
last 90 days (at most 5,000 messages per channel), every later sync reads what's new and
re-reads the last 48 hours, so recent edits and deletions show up; unchanged documents are
skipped by checksum at no cost.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import IntEnum
from typing import cast

from core.domain.documents import Section

DISCORD_EPOCH_MS = 1_420_070_400_000

# Permission bits (https://discord.com/developers/docs/topics/permissions).
ADMINISTRATOR = 1 << 3
VIEW_CHANNEL = 1 << 10
READ_MESSAGE_HISTORY = 1 << 16
ALL_PERMISSIONS = (1 << 53) - 1
READ = VIEW_CHANNEL | READ_MESSAGE_HISTORY
BOT_PERMISSIONS = READ  # what the install link asks for: read-only

WINDOW_GAP = timedelta(minutes=30)
WINDOW_MAX_MESSAGES = 50
BACKFILL_DAYS = 90
REREAD = timedelta(hours=48)
MAX_MESSAGES_PER_CHANNEL_PER_SYNC = 5_000
MAX_THREAD_MESSAGES = 500
MAX_THREADS_PER_SYNC = 300
MAX_CHANNELS_PER_SOURCE = 50
MAX_DOC_CHARS = 100_000
MAX_MESSAGE_CHARS = 4_000  # Discord allows 4,000 with Nitro
MAX_RECENT_WINDOWS = 200  # per channel, in the checkpoint
EMPTY_CONTENT_PROBE = 20  # this many fetched messages, all with no text: intent is off


class ChannelType(IntEnum):
    TEXT = 0
    VOICE = 2
    CATEGORY = 4
    ANNOUNCEMENT = 5
    ANNOUNCEMENT_THREAD = 10
    PUBLIC_THREAD = 11
    PRIVATE_THREAD = 12
    STAGE = 13
    FORUM = 15
    MEDIA = 16


MESSAGE_CHANNELS = frozenset({ChannelType.TEXT, ChannelType.ANNOUNCEMENT})
THREAD_ONLY_CHANNELS = frozenset({ChannelType.FORUM, ChannelType.MEDIA})
INDEXABLE_CHANNELS = MESSAGE_CHANNELS | THREAD_ONLY_CHANNELS

# Message types worth indexing: default, reply, slash-command output, thread starter.
# Joins, pins, boosts and the like are noise.
CONTENT_MESSAGE_TYPES = frozenset({0, 19, 20, 21, 23})

_SNOWFLAKE = re.compile(r"^[0-9]{15,21}$")


# ------------------------------------------------------------------ snowflakes


def is_snowflake(value: object) -> bool:
    return isinstance(value, str) and bool(_SNOWFLAKE.match(value)) and int(value) < 1 << 64


def snowflake_time(snowflake: str | int) -> datetime:
    """When a snowflake (any Discord id) was created."""
    ms = (int(snowflake) >> 22) + DISCORD_EPOCH_MS
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def snowflake_at(when: datetime) -> int:
    """The smallest snowflake created at ``when``: ``after=snowflake_at(t)`` means "since t"."""
    ms = int(when.timestamp() * 1000) - DISCORD_EPOCH_MS
    return max(ms, 0) << 22


# ------------------------------------------------------------------ server model


@dataclass(frozen=True, slots=True)
class Overwrite:
    id: str
    type: int  # 0 role, 1 member
    allow: int
    deny: int


@dataclass(frozen=True, slots=True)
class Role:
    id: str
    name: str
    permissions: int


@dataclass(frozen=True, slots=True)
class Guild:
    id: str
    name: str
    owner_id: str
    roles: tuple[Role, ...]
    icon: str | None = None


@dataclass(frozen=True, slots=True)
class Member:
    user_id: str
    roles: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Channel:
    id: str
    name: str
    type: int
    position: int = 0
    parent_id: str | None = None
    overwrites: tuple[Overwrite, ...] = ()

    @property
    def indexable(self) -> bool:
        return self.type in INDEXABLE_CHANNELS

    @property
    def has_messages(self) -> bool:
        return self.type in MESSAGE_CHANNELS


def base_permissions(guild: Guild, member: Member) -> int:
    """Server-wide permissions: the owner and Administrator get everything."""
    if member.user_id == guild.owner_id:
        return ALL_PERMISSIONS
    roles = {r.id: r for r in guild.roles}
    perms = roles[guild.id].permissions if guild.id in roles else 0  # @everyone's id = guild id
    for rid in member.roles:
        if (role := roles.get(rid)) is not None:
            perms |= role.permissions
    return ALL_PERMISSIONS if perms & ADMINISTRATOR else perms


def channel_permissions(guild: Guild, member: Member, channel: Channel) -> int:
    """Discord's documented order: base, then the @everyone overwrite, then all of the
    member's role overwrites together (deny first, then allow), then the member's own."""
    perms = base_permissions(guild, member)
    if perms & ADMINISTRATOR or perms == ALL_PERMISSIONS:
        return ALL_PERMISSIONS
    by_id = {o.id: o for o in channel.overwrites}
    if (everyone := by_id.get(guild.id)) is not None:
        perms = (perms & ~everyone.deny) | everyone.allow
    allow = deny = 0
    member_roles = set(member.roles)
    for o in channel.overwrites:
        if o.type == 0 and o.id in member_roles and o.id != guild.id:
            allow |= o.allow
            deny |= o.deny
    perms = (perms & ~deny) | allow
    own = by_id.get(member.user_id)
    if own is not None and own.type == 1:
        perms = (perms & ~own.deny) | own.allow
    return perms


def can_read(guild: Guild, member: Member, channel: Channel) -> bool:
    """View the channel and its history. A thread-only channel needs only View Channel
    to list its posts, but reading them still needs history, so both bits are required."""
    return channel_permissions(guild, member, channel) & READ == READ


def is_private_channel(guild: Guild, channel: Channel) -> bool:
    """Private: the @everyone role (with no other roles) can't view it."""
    everyone = Member(user_id="0", roles=())
    return not channel_permissions(guild, everyone, channel) & VIEW_CHANNEL


# ------------------------------------------------------------------ messages


@dataclass(frozen=True, slots=True)
class Message:
    id: str
    channel_id: str
    author_id: str
    author_name: str
    content: str
    type: int = 0
    attachments: tuple[str, ...] = ()  # file names only: contents are never fetched
    embeds: tuple[str, ...] = ()  # title/description text from rich embeds
    mentions: Mapping[str, str] = field(default_factory=dict[str, str])  # user id -> name
    reply_to: str | None = None  # the author name of the replied-to message
    edited: bool = False
    bot: bool = False

    @property
    def at(self) -> datetime:
        return snowflake_time(self.id)

    @property
    def indexable(self) -> bool:
        return self.type in CONTENT_MESSAGE_TYPES and bool(
            self.content.strip() or self.attachments or self.embeds
        )


@dataclass(frozen=True, slots=True)
class Thread:
    id: str
    parent_id: str
    name: str
    last_message_id: str | None
    archived: bool = False
    private: bool = False
    message_count: int = 0

    @property
    def last_activity(self) -> int:
        """Snowflake of the newest message (the thread id when it has none)."""
        return int(self.last_message_id or self.id)


_USER = re.compile(r"<@!?([0-9]{15,21})>")
_ROLE = re.compile(r"<@&([0-9]{15,21})>")
_CHANNEL = re.compile(r"<#([0-9]{15,21})>")
_EMOJI = re.compile(r"<a?:([A-Za-z0-9_]{1,32}):[0-9]{15,21}>")
_TIMESTAMP = re.compile(r"<t:(-?[0-9]{1,13})(?::[tTdDfFR])?>")


def render_content(
    message: Message,
    *,
    channels: Mapping[str, str] | None = None,
    roles: Mapping[str, str] | None = None,
) -> str:
    """Discord markup made readable: mentions become names, custom emoji their name,
    timestamps a date. Unknown ids stay generic (never a raw id that means nothing)."""
    text = message.content[:MAX_MESSAGE_CHARS]
    text = _USER.sub(lambda m: "@" + message.mentions.get(m.group(1), "someone"), text)
    text = _ROLE.sub(lambda m: "@" + (roles or {}).get(m.group(1), "role"), text)
    text = _CHANNEL.sub(lambda m: "#" + (channels or {}).get(m.group(1), "channel"), text)
    text = _EMOJI.sub(lambda m: f":{m.group(1)}:", text)

    def _ts(m: re.Match[str]) -> str:
        try:
            return datetime.fromtimestamp(int(m.group(1)), tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
        except (OverflowError, OSError, ValueError):
            return m.group(0)

    return _TIMESTAMP.sub(_ts, text)


def message_text(
    message: Message,
    *,
    channels: Mapping[str, str] | None = None,
    roles: Mapping[str, str] | None = None,
) -> str:
    """One message as plain lines: "Name (14:05): text", then attachments and embeds."""
    head = f"{message.author_name or 'unknown'} ({message.at:%H:%M})"
    if message.reply_to:
        head += f", replying to {message.reply_to}"
    lines = [f"{head}: {render_content(message, channels=channels, roles=roles).strip()}"]
    lines += [f"  [attachment: {name}]" for name in message.attachments[:10]]
    lines += [f"  [embed] {text[:1000]}" for text in message.embeds[:5] if text.strip()]
    if message.edited:
        lines[0] += " (edited)"
    return "\n".join(lines)


# ------------------------------------------------------------------ documents


@dataclass(frozen=True, slots=True)
class Window:
    channel_id: str
    messages: tuple[Message, ...]

    @property
    def first_id(self) -> str:
        return self.messages[0].id

    @property
    def last_id(self) -> str:
        return self.messages[-1].id


def group_windows(messages: Iterable[Message]) -> list[Window]:
    """Split one channel's messages into conversation windows.

    Deterministic for a given message list: sorted by id (= time), a new window starts
    after a ``WINDOW_GAP`` silence or when the current one has ``WINDOW_MAX_MESSAGES``.
    Messages with nothing to index are dropped first.
    """
    ordered = sorted((m for m in messages if m.indexable), key=lambda m: int(m.id))
    out: list[Window] = []
    current: list[Message] = []
    for m in ordered:
        if current and (len(current) >= WINDOW_MAX_MESSAGES or m.at - current[-1].at > WINDOW_GAP):
            out.append(Window(current[0].channel_id, tuple(current)))
            current = []
        current.append(m)
    if current:
        out.append(Window(current[0].channel_id, tuple(current)))
    return out


def window_external_id(channel_id: str, first_message_id: str) -> str:
    return f"msg:{channel_id}:{first_message_id}"


def thread_external_id(parent_id: str, thread_id: str) -> str:
    """Keyed by parent channel too, so a deleted channel's threads can be pruned by prefix."""
    return f"thread:{parent_id}:{thread_id}"


def channel_prefix(channel_id: str) -> str:
    return f"msg:{channel_id}:"


def thread_prefix(channel_id: str) -> str:
    return f"thread:{channel_id}:"


def message_url(guild_id: str, channel_id: str, message_id: str | None = None) -> str:
    base = f"https://discord.com/channels/{guild_id}/{channel_id}"
    return f"{base}/{message_id}" if message_id else base


def _cap(sections: list[Section]) -> tuple[Section, ...]:
    out: list[Section] = []
    used = 0
    for s in sections:
        if used + len(s.text) > MAX_DOC_CHARS:
            room = MAX_DOC_CHARS - used
            if room > 0:
                out.append(Section(s.heading_path, s.text[:room] + "\n(truncated)"))
            break
        out.append(s)
        used += len(s.text)
    return tuple(out)


def window_sections(
    window: Window,
    *,
    guild_name: str,
    channel_name: str,
    channels: Mapping[str, str] | None = None,
    roles: Mapping[str, str] | None = None,
) -> tuple[Section, ...]:
    """Sections are built directly, not parsed from Markdown: message text can't forge a
    heading or change the document's structure."""
    heading = f"{guild_name} > #{channel_name} > {window.messages[0].at:%Y-%m-%d}"
    body = "\n".join(message_text(m, channels=channels, roles=roles) for m in window.messages)
    return _cap([Section(heading, body)])


def thread_sections(
    thread: Thread,
    messages: Sequence[Message],
    *,
    guild_name: str,
    parent_name: str,
    channels: Mapping[str, str] | None = None,
    roles: Mapping[str, str] | None = None,
) -> tuple[Section, ...]:
    heading = f"{guild_name} > #{parent_name} > {thread.name.strip() or 'thread'}"
    ordered = sorted((m for m in messages if m.indexable), key=lambda m: int(m.id))
    body = "\n".join(message_text(m, channels=channels, roles=roles) for m in ordered)
    return _cap([Section(heading, body)])


def window_title(channel_name: str, window: Window) -> str:
    return f"#{channel_name} · {window.messages[0].at:%b %d, %Y %H:%M}"


def thread_title(thread: Thread, parent_name: str) -> str:
    return f"{thread.name.strip() or 'Thread'} (#{parent_name})"


def authors(messages: Iterable[Message], limit: int = 5) -> str | None:
    """The most active authors, for the document's author field."""
    counts: dict[str, int] = {}
    for m in messages:
        if m.author_name:
            counts[m.author_name] = counts.get(m.author_name, 0) + 1
    if not counts:
        return None
    top = sorted(counts, key=lambda n: (-counts[n], n))[:limit]
    return ", ".join(top)


def checksum(sections: Sequence[Section], title: str) -> str:
    h = hashlib.sha256(title.encode())
    for s in sections:
        h.update(b"\x00" + (s.heading_path or "").encode() + b"\x00" + s.text.encode())
    return h.hexdigest()


def intent_looks_off(messages: Sequence[Message]) -> bool:
    """Without the Message Content intent Discord returns messages with empty text (and no
    attachments or embeds). Many such messages and none with text means it's off."""
    if len(messages) < EMPTY_CONTENT_PROBE:
        return False
    return not any(m.content.strip() or m.attachments or m.embeds for m in messages)


# ------------------------------------------------------------------ checkpoint


@dataclass(frozen=True, slots=True)
class ChannelCursor:
    """Per-channel resume point.

    ``last_id`` is the newest message read; ``recent`` the windows (first id, last id)
    ending inside the re-read period, always including the newest one. The next sync
    re-reads from the first of those, so a window still growing is rebuilt whole.

    ``threads_floor`` is a snowflake: this channel's threads with activity after it are
    (re)read. After a full pass it moves to ``REREAD`` before that pass started; a pass
    cut short (deadline, cap, a failed thread) leaves it at the last thread indexed. Per
    channel, so a channel added later still gets its own 90-day backfill.
    """

    last_id: str | None = None
    recent: tuple[tuple[str, str], ...] = ()
    threads_floor: int | None = None
    backlog: bool = False  # the last pass hit the cap: resume from last_id, skip the re-read

    def anchor(self, cutoff: int) -> str | None:
        """First message id to re-read from (None: read everything after ``last_id``)."""
        for first, last in self.recent:
            if int(last) >= cutoff:
                return first
        return self.recent[-1][0] if self.recent else None

    def as_json(self) -> dict[str, object]:
        return {
            "last_id": self.last_id,
            "recent": [list(w) for w in self.recent],
            "threads_floor": str(self.threads_floor) if self.threads_floor is not None else None,
            "backlog": self.backlog,
        }

    @classmethod
    def from_json(cls, raw: object) -> ChannelCursor:
        data = _obj(raw)
        recent: list[tuple[str, str]] = []
        for w in _items(data.get("recent")):
            pair = _items(w)
            if len(pair) == 2 and is_snowflake(pair[0]) and is_snowflake(pair[1]):
                recent.append((str(pair[0]), str(pair[1])))
        last, floor = data.get("last_id"), data.get("threads_floor")
        return cls(
            last_id=str(last) if is_snowflake(last) else None,
            recent=tuple(recent),
            threads_floor=int(floor) if isinstance(floor, str) and floor.isdigit() else None,
            backlog=data.get("backlog") is True,
        )


def _obj(raw: object) -> dict[str, object]:
    """A JSON object from stored data, or {} for anything else (fail soft: a bad checkpoint
    means a fresh sync, never a crash)."""
    if not isinstance(raw, dict):
        return {}
    return {str(k): v for k, v in cast(dict[object, object], raw).items()}


def _items(raw: object) -> list[object]:
    return list(cast(list[object], raw)) if isinstance(raw, list) else []


def recent_windows(
    windows: Sequence[Window], cutoff: int, previous: Sequence[tuple[str, str]] = ()
) -> tuple[tuple[str, str], ...]:
    """Windows to remember for the next re-read: those ending at or after ``cutoff`` plus
    the newest one, merged with still-recent ones from before this pass, capped."""
    merged = dict(previous)
    merged.update({w.first_id: w.last_id for w in windows})
    ordered = sorted(merged.items(), key=lambda kv: int(kv[0]))
    keep = [(f, last) for f, last in ordered if int(last) >= cutoff]
    if ordered and (not keep or keep[-1] != ordered[-1]):
        keep.append(ordered[-1])
    return tuple(keep[-MAX_RECENT_WINDOWS:])


@dataclass(frozen=True, slots=True)
class SyncCheckpoint:
    """The source's ``checkpoint`` JSON: one cursor per channel."""

    channels: Mapping[str, ChannelCursor] = field(default_factory=dict[str, ChannelCursor])

    def cursor(self, channel_id: str) -> ChannelCursor:
        return self.channels.get(channel_id, ChannelCursor())

    def with_cursor(self, channel_id: str, cursor: ChannelCursor) -> SyncCheckpoint:
        return replace(self, channels={**self.channels, channel_id: cursor})

    def without(self, channel_id: str) -> SyncCheckpoint:
        return replace(self, channels={k: v for k, v in self.channels.items() if k != channel_id})

    @property
    def first_sync(self) -> bool:
        return not any(c.last_id or c.threads_floor for c in self.channels.values())

    def as_json(self) -> dict[str, object]:
        return {"channels": {cid: c.as_json() for cid, c in self.channels.items()}}

    @classmethod
    def from_json(cls, raw: object) -> SyncCheckpoint:
        channels = {
            cid: ChannelCursor.from_json(c)
            for cid, c in _obj(_obj(raw).get("channels")).items()
            if is_snowflake(cid)
        }
        return cls(channels=channels)


# ------------------------------------------------------------------ connect


@dataclass(frozen=True, slots=True)
class ChannelChoice:
    """One row of the channel picker."""

    channel: Channel
    category: str | None
    private: bool
    user_can_read: bool
    bot_can_read: bool
    connected: bool = False

    @property
    def selectable(self) -> bool:
        return self.channel.indexable and self.user_can_read and self.bot_can_read


def picker_rows(
    guild: Guild,
    channels: Sequence[Channel],
    *,
    user_readable: AbstractSet[str],
    bot: Member,
    connected: AbstractSet[str] = frozenset(),
) -> list[ChannelChoice]:
    """Indexable channels in Discord's order (category position, then channel position),
    with why a channel can't be picked."""
    by_id = {c.id: c for c in channels}
    rows: list[ChannelChoice] = []
    for c in channels:
        if not c.indexable:
            continue
        parent = by_id.get(c.parent_id) if c.parent_id else None
        rows.append(
            ChannelChoice(
                channel=c,
                category=parent.name if parent else None,
                private=is_private_channel(guild, c),
                user_can_read=c.id in user_readable,
                bot_can_read=can_read(guild, bot, c),
                connected=c.id in connected,
            )
        )

    def order(r: ChannelChoice) -> tuple[int, int, int, int]:
        parent = by_id.get(r.channel.parent_id) if r.channel.parent_id else None
        return (
            parent.position if parent else -1,
            int(parent.id) if parent else 0,
            r.channel.position,
            int(r.channel.id),
        )

    return sorted(rows, key=order)


def source_name(guild_name: str, guild_id: str, taken: AbstractSet[str]) -> str:
    """A source name unique in the workspace (two servers can share a name)."""
    base = (guild_name.strip() or "Discord server")[:80]
    return base if base not in taken else f"{base} ({guild_id[-6:]})"


INSTALL_STATE_PURPOSE = "discord-install"
GRANT_PURPOSE = "discord-grant"
