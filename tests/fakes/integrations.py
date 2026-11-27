"""In-memory ConnectionStore and a scripted OAuthProvider (ADR-0032)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

from core.domain.errors import ReconnectRequired, UpstreamUnavailable, ValidationFailed
from core.domain.ids import UserId, WorkspaceId
from core.domain.integrations import Account, ConnectionStatus, OAuthSpec, TokenSet
from core.ports.integrations import ClientCredentials, Connection


class InMemoryConnections:
    def __init__(self, *, available: bool = True, now: datetime | None = None) -> None:
        self.available = available
        self.rows: dict[UUID, Connection] = {}
        self.secrets: dict[UUID, TokenSet] = {}
        self.leases: dict[UUID, datetime] = {}
        self._now = now or datetime(2026, 11, 27, tzinfo=UTC)

    async def save(
        self, ws: WorkspaceId, user: UserId, provider: str, account: Account, tokens: TokenSet
    ) -> Connection:
        for c in self.rows.values():
            if (c.workspace_id, c.user_id, c.provider, c.account_id) == (
                ws, user, provider, account.id,
            ):  # fmt: skip
                updated = replace(
                    c, account_label=account.label, scopes=tokens.scopes,
                    status=ConnectionStatus.CONNECTED, expires_at=tokens.expires_at,
                    last_error=None,
                )  # fmt: skip
                self.rows[c.id], self.secrets[c.id] = updated, tokens
                return updated
        c = Connection(
            id=uuid4(), workspace_id=ws, user_id=user, provider=provider, account_id=account.id,
            account_label=account.label, scopes=tokens.scopes,
            status=ConnectionStatus.CONNECTED, expires_at=tokens.expires_at, last_error=None,
            created_at=self._now,
        )  # fmt: skip
        self.rows[c.id], self.secrets[c.id] = c, tokens
        return c

    async def list_for_user(self, ws: WorkspaceId, user: UserId) -> list[Connection]:
        return [c for c in self.rows.values() if (c.workspace_id, c.user_id) == (ws, user)]

    async def get(self, ws: WorkspaceId, user: UserId, connection_id: UUID) -> Connection | None:
        c = self.rows.get(connection_id)
        return c if c is not None and (c.workspace_id, c.user_id) == (ws, user) else None

    async def by_id(self, connection_id: UUID) -> Connection | None:
        return self.rows.get(connection_id)

    async def tokens(self, connection_id: UUID) -> TokenSet | None:
        return self.secrets.get(connection_id)

    async def try_lease(self, connection_id: UUID, until: datetime) -> bool:
        if connection_id in self.leases:
            return False
        self.leases[connection_id] = until
        return True

    async def store_refresh(self, connection_id: UUID, tokens: TokenSet) -> None:
        c = self.rows[connection_id]
        self.rows[connection_id] = replace(
            c, status=ConnectionStatus.CONNECTED, last_error=None, expires_at=tokens.expires_at
        )
        self.secrets[connection_id] = tokens
        self.leases.pop(connection_id, None)

    async def mark(self, connection_id: UUID, status: ConnectionStatus, error: str | None) -> None:
        c = self.rows[connection_id]
        self.rows[connection_id] = replace(c, status=status, last_error=error)
        self.leases.pop(connection_id, None)

    async def delete(self, ws: WorkspaceId, user: UserId, connection_id: UUID) -> bool:
        if await self.get(ws, user, connection_id) is None:
            return False
        del self.rows[connection_id]
        self.secrets.pop(connection_id, None)
        return True


class FakeOAuth:
    """Records calls; ``refresh_outcome`` decides what a refresh does."""

    def __init__(
        self,
        tokens: TokenSet | None = None,
        account: Account | None = None,
    ) -> None:
        self.tokens = tokens or TokenSet("access-1", "refresh-1", None, ("read",))
        self.account = account or Account("acct-1", "ada@example.test")
        self.exchanged: list[tuple[str, str | None, str]] = []  # code, verifier, redirect
        self.refreshed: list[str] = []
        self.revoked: list[str] = []
        self.refresh_outcome: TokenSet | Exception = TokenSet("access-2", "refresh-2")
        self.refuse_code = False

    async def exchange(
        self, spec: OAuthSpec, client: ClientCredentials, code: str, verifier: str | None,
        redirect_uri: str,
    ) -> tuple[TokenSet, Account]:  # fmt: skip
        self.exchanged.append((code, verifier, redirect_uri))
        if self.refuse_code:
            raise ValidationFailed("refused")
        return self.tokens, self.account

    async def refresh(
        self, spec: OAuthSpec, client: ClientCredentials, refresh_token: str
    ) -> TokenSet:
        self.refreshed.append(refresh_token)
        if isinstance(self.refresh_outcome, Exception):
            raise self.refresh_outcome
        return self.refresh_outcome

    async def revoke(self, spec: OAuthSpec, client: ClientCredentials, token: str) -> None:
        self.revoked.append(token)


def terminal() -> ReconnectRequired:
    return ReconnectRequired("revoked", connector="test")


def transient() -> UpstreamUnavailable:
    return UpstreamUnavailable("down")
