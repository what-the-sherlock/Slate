"""Outgoing transactional email (verification and password-reset codes)."""

from __future__ import annotations

from typing import Protocol

from core.domain.emails import OutgoingEmail


class EmailSender(Protocol):
    async def send(self, email: OutgoingEmail) -> None:
        """Deliver one plain-text email.

        Raises UpstreamUnavailable (provider down, timeout) or RateLimited (provider quota).
        Implementations never log the body: it contains a one-time code.
        """
        ...
