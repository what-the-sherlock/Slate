"""What Slate needs from a mailbox (ADR-0028). The adapter speaks IMAP, read-only.

Connectors only fetch (rule 2): the adapter parses each message into a ``Mail`` at the edge
and never writes. Errors: PermissionDenied for a rejected login (wrong address or app
password, IMAP turned off); UpstreamUnavailable for anything else (network, server).
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Protocol

from core.domain.mail import Folder, Mail, Provider


class MailSession(Protocol):
    async def folders(self) -> list[Folder]:
        """Inbox and the Sent folder (special-use flag or well-known name)."""
        ...

    async def open_folder(self, name: str) -> int:
        """EXAMINE (read-only) a folder; returns its UIDVALIDITY."""
        ...

    async def uids_since(self, since: datetime) -> list[int]:
        """UIDs of messages received on or after this day, ascending."""
        ...

    async def uids_after(self, uid: int) -> list[int]:
        """UIDs greater than ``uid``, ascending."""
        ...

    async def fetch(self, uids: Sequence[int]) -> list[Mail]:
        """Parsed messages (BODY.PEEK: nothing is marked read). Oversized or unparseable
        messages are skipped, not raised."""
        ...

    async def attachment(self, uid: int, part: int) -> tuple[str, bytes] | None:
        """(Message-ID, bytes) of one attachment of a message in the open folder (BODY.PEEK),
        or None when the message or part is gone or over the size cap (ADR-0043)."""
        ...

    async def thread_uids(self, mail: Mail) -> list[int]:
        """UIDs in the open folder that belong to ``mail``'s conversation."""
        ...


class Mailbox(Protocol):
    def session(
        self, provider: Provider, address: str, password: str
    ) -> AbstractAsyncContextManager[MailSession]:
        """Log in (TLS, certificate checked) and log out at the end."""
        ...
