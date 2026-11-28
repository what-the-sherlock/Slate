"""On-demand attachment fetchers, one per connector that carries files (ADR-0043).

Each only fetches (rule 2) through its connector's port; ``AttachmentReader`` sniffs,
parses and stores. A file that can't be found any more is ``None`` (shown as "no longer
available"); temporary trouble raises, and the file stays ``listed`` for a later read.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from core.domain.attachments import (
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENTS_LISTED,
    AttachmentInfo,
    key_seed,
)
from core.domain.discord import (
    MAX_THREAD_MESSAGES,
    WINDOW_MAX_MESSAGES,
    group_windows,
    is_snowflake,
    parse_document_id,
)
from core.domain.documents import AttachmentParent
from core.domain.errors import NotFound, PermissionDenied
from core.domain.mail import Provider
from core.ports.connected import ConnectionAccess, SourceConnector
from core.ports.credentials import CredentialStore
from core.ports.discord import DiscordApi
from core.ports.mailbox import Mailbox
from core.ports.sources import SourceStore


@runtime_checkable
class FileConnector(Protocol):
    """A ``SourceConnector`` that can fetch one attachment again by its ref (Gmail)."""

    async def fetch_attachment(self, token: str, ref: Mapping[str, str]) -> bytes | None: ...


def _same_message(info: AttachmentInfo, message_id: str) -> bool:
    """An email attachment's key starts with its message's Message-ID hash."""
    return info.key.startswith(key_seed(message_id) + "-")


class ConnectedFiles:
    """Gmail (and any OAuth connector implementing ``FileConnector``)."""

    def __init__(
        self,
        sources: SourceStore,
        access: ConnectionAccess,
        connectors: Mapping[str, SourceConnector],
    ) -> None:
        self._sources = sources
        self._access = access
        self._connectors = dict(connectors)

    def connectors(self) -> list[str]:
        return [n for n, c in self._connectors.items() if isinstance(c, FileConnector)]

    async def fetch(self, parent: AttachmentParent, info: AttachmentInfo) -> bytes | None:
        source = await self._sources.get_source(parent.workspace_id, parent.source_id)
        connector = self._connectors.get(parent.connector)
        if source is None or source.connection_id is None:
            return None
        if not isinstance(connector, FileConnector):
            return None
        token = await self._access.access_token(source.connection_id)
        return await connector.fetch_attachment(token, info.ref)

    async def discover(self, parent: AttachmentParent) -> list[AttachmentInfo]:
        return []  # Gmail lists files at sync (an older checkpoint re-backfills once)


class DiscordFiles:
    """Discord: never read at sync. The message is read again for a fresh signed link
    (links expire), then the file is downloaded from Discord's CDN, byte-capped."""

    def __init__(self, dc: DiscordApi) -> None:
        self._dc = dc

    async def fetch(self, parent: AttachmentParent, info: AttachmentInfo) -> bytes | None:
        channel, message, att = (info.ref.get(k, "") for k in ("channel", "message", "attachment"))
        if not (is_snowflake(channel) and is_snowflake(message) and is_snowflake(att)):
            return None
        try:
            found = [m async for m in self._dc.messages_after(channel, int(message) - 1, 1)]
        except (NotFound, PermissionDenied):
            return None  # channel gone, or the bot can no longer read it
        if not found or found[0].id != message:
            return None  # the message was deleted
        url = found[0].file_urls.get(att)
        if not url:
            return None  # the attachment was removed from the message
        return await self._dc.download(url, MAX_ATTACHMENT_BYTES)

    async def discover(self, parent: AttachmentParent) -> list[AttachmentInfo]:
        """A document indexed before files were listed: read its messages again. A window
        is rebuilt exactly as the sync grouped it (from its first message)."""
        parsed = parse_document_id(parent.external_id)
        if parsed is None:
            return []
        kind, channel, rest = parsed
        try:
            if kind == "msg":
                got = [
                    m
                    async for m in self._dc.messages_after(
                        channel, int(rest) - 1, WINDOW_MAX_MESSAGES
                    )
                ]
                windows = group_windows(got)
                messages = windows[0].messages if windows and windows[0].first_id == rest else ()
            else:  # a thread is a channel of its own
                messages = tuple(
                    [m async for m in self._dc.messages_after(rest, 0, MAX_THREAD_MESSAGES)]
                )
        except (NotFound, PermissionDenied):
            return []
        return [f for m in messages for f in m.files][:MAX_ATTACHMENTS_LISTED]


class MailboxFiles:
    """IMAP: the message is read again (read-only, BODY.PEEK) by folder and UID, and
    must still carry the same Message-ID (a rebuilt mailbox reuses UIDs)."""

    def __init__(
        self, mailbox: Mailbox, credentials: CredentialStore, sources: SourceStore
    ) -> None:
        self._mailbox = mailbox
        self._credentials = credentials
        self._sources = sources

    async def fetch(self, parent: AttachmentParent, info: AttachmentInfo) -> bytes | None:
        source = await self._sources.get_source(parent.workspace_id, parent.source_id)
        if source is None or source.credential_id is None:
            return None
        folder = info.ref.get("folder", "")
        try:
            uid, part = int(info.ref.get("uid", "")), int(info.ref.get("part", ""))
        except ValueError:
            return None
        if not folder or uid <= 0 or part < 0:
            return None
        secret = await self._credentials.secret(source.workspace_id, source.credential_id)
        if secret is None:
            raise PermissionDenied("Reconnect this mailbox to read its attachments.")
        try:
            provider = Provider(str(source.config.get("provider")))
        except ValueError:
            return None
        address = str(source.config.get("address", ""))
        async with self._mailbox.session(provider, address, secret) as session:
            await session.open_folder(folder)
            got = await session.attachment(uid, part)
        if got is None or not _same_message(info, got[0]):
            return None
        return got[1]

    async def discover(self, parent: AttachmentParent) -> list[AttachmentInfo]:
        return []  # listed at sync (an older checkpoint re-reads once)
