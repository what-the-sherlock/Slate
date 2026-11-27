"""The connector port for OAuth-connected sources (ADR-0033)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Protocol
from uuid import UUID

from core.domain.auth import Identity
from core.domain.connected import Page


class SourceConnector(Protocol):
    """Fetches one page of a provider's items with an access token. Never parses beyond
    shaping items into sections, never writes anything (CLAUDE.md rule 2).

    Raises ReconnectRequired (the token was refused: the user must sign in again),
    RateLimited (with ``retry_after_s``), UpstreamUnavailable, or ConnectorError.
    """

    async def pull(
        self, token: str, checkpoint: Mapping[str, object], *, now: datetime
    ) -> Page: ...


class ConnectionAccess(Protocol):
    """What the sync engine needs from integrations (implemented by IntegrationService)."""

    async def access_token(self, connection_id: UUID) -> str: ...

    async def disconnect(self, identity: Identity, connection_id: UUID) -> None: ...
