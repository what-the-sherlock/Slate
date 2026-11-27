"""Integration ports (ADR-0032): stored connections and the OAuth provider calls."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from core.domain.ids import UserId, WorkspaceId
from core.domain.integrations import Account, ConnectionStatus, OAuthSpec, TokenSet


@dataclass(frozen=True, slots=True)
class ClientCredentials:
    """This deployment's OAuth app for a provider (from env, ``core/config.py``)."""

    client_id: str
    client_secret: str


@dataclass(frozen=True, slots=True)
class Connection:
    """A user's signed-in account at a provider, in one workspace. Never carries tokens:
    those stay encrypted in the store until ``tokens`` is called."""

    id: UUID
    workspace_id: WorkspaceId
    user_id: UserId
    provider: str
    account_id: str
    account_label: str | None
    scopes: tuple[str, ...]
    status: ConnectionStatus
    expires_at: datetime | None
    last_error: str | None
    created_at: datetime


class ConnectionStore(Protocol):
    @property
    def available(self) -> bool:
        """False without an encryption key: OAuth connections can't be stored."""
        ...

    async def save(
        self,
        ws: WorkspaceId,
        user: UserId,
        provider: str,
        account: Account,
        tokens: TokenSet,
    ) -> Connection:
        """Insert, or replace the tokens of, this user's connection to that account
        (a reconnect), and mark it connected."""
        ...

    async def list_for_user(self, ws: WorkspaceId, user: UserId) -> list[Connection]: ...

    async def get(self, ws: WorkspaceId, user: UserId, connection_id: UUID) -> Connection | None:
        """Only the owner's own connection: None for anyone else's (no probing)."""
        ...

    async def by_id(self, connection_id: UUID) -> Connection | None:
        """For background work, which acts for the connection's owner."""
        ...

    async def tokens(self, connection_id: UUID) -> TokenSet | None:
        """The decrypted tokens, or None if gone or no longer decryptable."""
        ...

    async def try_lease(self, connection_id: UUID, until: datetime) -> bool:
        """Take the refresh lease (one refresher at a time: providers rotate refresh
        tokens, so two concurrent refreshes could kill the grant)."""
        ...

    async def store_refresh(self, connection_id: UUID, tokens: TokenSet) -> None:
        """Save refreshed tokens, mark connected, clear the error and the lease."""
        ...

    async def mark(self, connection_id: UUID, status: ConnectionStatus, error: str | None) -> None:
        """Record a failed use (and release the lease)."""
        ...

    async def delete(self, ws: WorkspaceId, user: UserId, connection_id: UUID) -> bool: ...


class OAuthProvider(Protocol):
    """The calls to a provider's OAuth endpoints. Adapters never retry the code exchange
    or a refresh (both are single use), and classify failures (domain rules)."""

    async def exchange(
        self, spec: OAuthSpec, client: ClientCredentials, code: str, verifier: str | None,
        redirect_uri: str,
    ) -> tuple[TokenSet, Account]:  # fmt: skip
        """Redeem a code. Raises ValidationFailed (the provider refused it) or
        UpstreamUnavailable."""
        ...

    async def refresh(
        self, spec: OAuthSpec, client: ClientCredentials, refresh_token: str
    ) -> TokenSet:
        """Raises ReconnectRequired (terminal) or UpstreamUnavailable (transient)."""
        ...

    async def revoke(self, spec: OAuthSpec, client: ClientCredentials, token: str) -> None:
        """Best effort: never raises."""
        ...
