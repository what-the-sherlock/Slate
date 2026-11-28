"""Reading an attachment again, on demand (ADR-0043).

One implementation per connector that carries files. It only fetches (rule 2): the
engine (``core.services.attachments``) sniffs, parses and stores what it returns.
"""

from __future__ import annotations

from typing import Protocol

from core.domain.attachments import AttachmentInfo
from core.domain.documents import AttachmentParent


class AttachmentFetcher(Protocol):
    async def fetch(self, parent: AttachmentParent, info: AttachmentInfo) -> bytes | None:
        """The file's bytes, or None when it no longer exists or can't be reached any
        more (deleted, access lost). Raises UnreadableFile(``too_large``) past the cap,
        and RateLimited / UpstreamUnavailable / ReconnectRequired for temporary trouble."""
        ...

    async def discover(self, parent: AttachmentParent) -> list[AttachmentInfo]:
        """The parent's files when it was indexed before they were listed (Tier 0
        missing). Empty when there are none or the connector can't tell."""
        ...
