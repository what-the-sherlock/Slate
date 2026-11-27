"""Integrations use cases (ADR-0032): the catalogue, the OAuth connect flow, disconnect,
and fresh access tokens for whatever uses a connection (sync jobs, live lookups).

Rules:
* A connection is **personal**: it belongs to one member of one workspace, and only they
  see, use or remove it. Connecting needs a verified account with a role above viewer.
* The flow is ``start`` -> provider -> web callback page -> ``complete``. ``complete``
  needs the same signed-in user and workspace the state was issued to, within 10 minutes.
* ``access_token`` refreshes ahead of expiry under a lease. A dead grant marks the
  connection ``needs_reconnect`` and raises ReconnectRequired; a transient failure
  keeps the token (it may still be valid) and raises UpstreamUnavailable.
* Nothing here logs a token, a code or a state.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from uuid import UUID

import structlog

from core.domain.auth import Identity, Role
from core.domain.errors import (
    NotFound,
    PermissionDenied,
    RateLimited,
    ReconnectRequired,
    UpstreamUnavailable,
    ValidationFailed,
)
from core.domain.ids import SourceId
from core.domain.integrations import (
    REFRESH_LEASE,
    REGISTRY,
    STATE_PURPOSE,
    STATE_TTL,
    AuthKind,
    ConnectionStatus,
    IntegrationState,
    OAuthSpec,
    ProviderSpec,
    TokenSet,
    authorize_url,
    code_challenge,
    connection_state,
    derive_verifier,
    needs_refresh,
    new_nonce,
)
from core.domain.signed_tokens import sign, verify
from core.ports.integrations import ClientCredentials, Connection, ConnectionStore, OAuthProvider
from core.ports.rate_limit import Limit, RateLimiter
from core.ports.sources import SourceStore
from core.ports.system import Clock

log = structlog.get_logger(__name__)

CONNECT_LIMIT = Limit(budget=20, window=timedelta(hours=1))
MAX_CODE_CHARS = 2048
ConnectedHook = Callable[[Connection], Awaitable[object]]
EXPIRED = "This sign-in link has expired or isn't yours. Start connecting again."


@dataclass(frozen=True, slots=True)
class IntegrationView:
    """One provider on the Integrations page, for the signed-in user."""

    spec: ProviderSpec
    state: IntegrationState
    connections: tuple[Connection, ...] = ()
    sources: int = 0  # sources created by the older install flows (GitHub, Discord, email)
    # Each connection's synced source (ADR-0033), for "Open source" and its status.
    source_ids: Mapping[UUID, SourceId] = field(default_factory=dict[UUID, SourceId])
    warnings: tuple[str, ...] = ()


class IntegrationService:
    def __init__(
        self,
        connections: ConnectionStore,
        oauth: OAuthProvider,
        sources: SourceStore,
        limiter: RateLimiter,
        clock: Clock,
        *,
        secret: bytes,
        clients: Mapping[str, ClientCredentials],
        redirect_uri: str | None,
        legacy_ready: Mapping[str, bool],
        registry: Sequence[ProviderSpec] = REGISTRY,
    ) -> None:
        self._connections = connections
        self._oauth = oauth
        self._sources = sources
        self._limiter = limiter
        self._clock = clock
        self._secret = secret
        self._clients = clients
        self._redirect_uri = redirect_uri
        self._legacy_ready = legacy_ready
        self._registry = tuple(registry)
        self._by_id = {p.id: p for p in self._registry}
        self._hooks: dict[str, ConnectedHook] = {}

    def on_connected(self, provider_id: str, hook: ConnectedHook) -> None:
        """Run ``hook`` after every sign-in or reconnect to this provider (the sync engine
        creates the source and queues its first sync). Wired at the composition root."""
        self._hooks[provider_id] = hook

    # ------------------------------------------------------------ catalogue

    async def catalog(self, identity: Identity) -> list[IntegrationView]:
        mine = await self._connections.list_for_user(identity.workspace_id, identity.user_id)
        views: list[IntegrationView] = []
        for spec in self._registry:
            if spec.auth is AuthKind.OAUTH2:
                views.append(await self._oauth_view(identity, spec, mine))
            else:
                views.append(await self._legacy_view(identity, spec))
        return views

    async def _oauth_view(
        self, identity: Identity, spec: ProviderSpec, mine: list[Connection]
    ) -> IntegrationView:
        own = tuple(c for c in mine if c.provider == spec.id)
        if not spec.available and not own:
            return IntegrationView(spec, IntegrationState.COMING_SOON)
        if self._client(spec) is None and not own:
            return IntegrationView(spec, IntegrationState.NOT_SET_UP)
        source_ids: dict[UUID, SourceId] = {}
        if own:
            for src in await self._sources.sources_by_connector(identity.workspace_id, spec.id):
                if src.connection_id is not None and src.created_by == identity.user_id:
                    source_ids[src.connection_id] = src.id
        state = connection_state([c.status for c in own])
        warnings = await self._warnings(identity, spec, own)
        return IntegrationView(spec, state, own, source_ids=source_ids, warnings=warnings)

    async def _warnings(
        self, identity: Identity, spec: ProviderSpec, own: tuple[Connection, ...]
    ) -> tuple[str, ...]:
        """Gmail connected *and* the same mailbox over IMAP: everything is indexed twice."""
        if spec.id != "gmail" or not own:
            return ()
        mine = {(c.account_label or "").lower() for c in own}
        imap = await self._sources.sources_by_connector(identity.workspace_id, "imap")
        twice = sorted(
            str(s.config.get("address", "")) for s in imap
            if s.created_by == identity.user_id
            and str(s.config.get("address", "")).lower() in mine
        )  # fmt: skip
        return tuple(
            f"{a} is also connected with an app password, so its mail is indexed twice. "
            "Disconnect the Email (IMAP) source to keep one copy."
            for a in twice
        )

    async def _legacy_view(self, identity: Identity, spec: ProviderSpec) -> IntegrationView:
        count = 0
        for connector in spec.connectors:
            found = await self._sources.sources_by_connector(identity.workspace_id, connector)
            count += sum(
                1 for s in found if s.created_by == identity.user_id and s.status != "deleting"
            )
        if count:
            return IntegrationView(spec, IntegrationState.CONNECTED, sources=count)
        if not self._legacy_ready.get(spec.id, False):
            return IntegrationView(spec, IntegrationState.NOT_SET_UP)
        return IntegrationView(spec, IntegrationState.NOT_CONNECTED)

    # ------------------------------------------------------------ connect

    async def start(self, identity: Identity, provider_id: str) -> str:
        """The provider's sign-in URL for this user. Raises NotFound (unknown provider),
        PermissionDenied, ValidationFailed (not connectable here) or RateLimited."""
        spec, oauth, client, redirect = self._connectable(identity, provider_id)
        if not await self._limiter.try_spend(f"connect:{identity.user_id}", 1, CONNECT_LIMIT):
            raise RateLimited("Too many sign-ins. Try again in a while.", retry_after_s=3600)
        nonce = new_nonce()
        state = sign(
            {"u": str(identity.user_id), "w": str(identity.workspace_id), "p": spec.id, "n": nonce},
            secret=self._secret,
            purpose=STATE_PURPOSE,
            expires_at=self._clock.now() + STATE_TTL,
        )
        verifier = derive_verifier(self._secret, nonce) if oauth.pkce else None
        log.info("integration.connect_started", provider=spec.id)
        return authorize_url(
            oauth,
            client_id=client.client_id,
            redirect_uri=redirect,
            state=state,
            challenge=code_challenge(verifier) if verifier else None,
        )

    async def complete(
        self, identity: Identity, provider_id: str, code: str, state: str
    ) -> Connection:
        """Redeem the provider's code and store the connection. The state must be the one
        ``start`` issued to this user, workspace and provider."""
        spec, oauth, client, redirect = self._connectable(identity, provider_id)
        if not code or len(code) > MAX_CODE_CHARS:
            raise ValidationFailed("The sign-in didn't return a usable code. Try again.")
        try:
            claims = verify(
                state, secret=self._secret, purpose=STATE_PURPOSE, now=self._clock.now()
            )
        except ValidationFailed as e:
            raise ValidationFailed(EXPIRED) from e
        expected = {"u": str(identity.user_id), "w": str(identity.workspace_id), "p": spec.id}
        if any(claims.get(k) != v for k, v in expected.items()):
            raise ValidationFailed(EXPIRED)
        nonce = claims.get("n")
        if not isinstance(nonce, str) or not nonce:
            raise ValidationFailed(EXPIRED)
        verifier = derive_verifier(self._secret, nonce) if oauth.pkce else None
        tokens, account = await self._oauth.exchange(oauth, client, code, verifier, redirect)
        await self._check_grant(spec, oauth, client, tokens)
        connection = await self._connections.save(
            identity.workspace_id, identity.user_id, spec.id, account, tokens
        )
        log.info("integration.connected", provider=spec.id, connection_id=str(connection.id))
        hook = self._hooks.get(spec.id)
        if hook is not None:
            await hook(connection)  # never raises: the connection is already saved
        return connection

    async def _check_grant(
        self, spec: ProviderSpec, oauth: OAuthSpec, client: ClientCredentials, tokens: TokenSet
    ) -> None:
        """Refuse a sign-in that can't work, and revoke it so nothing is left behind:
        a required permission was unticked on the consent screen, or no refresh token came
        back (the connection would die within the hour)."""
        missing = [s for s in oauth.required_scopes if tokens.scopes and s not in tokens.scopes]
        if missing:
            await self._oauth.revoke(oauth, client, tokens.access_token)
            log.info("integration.scope_missing", provider=spec.id)
            raise ValidationFailed(
                f"{spec.name} wasn't connected: Slate needs every permission it asks for. "
                "Connect again and leave all the boxes ticked on the sign-in screen."
            )
        if oauth.requires_refresh and not tokens.refresh_token:
            await self._oauth.revoke(oauth, client, tokens.access_token)
            log.info("integration.no_refresh_token", provider=spec.id)
            raise ValidationFailed(
                f"{spec.name} didn't grant lasting access. Remove Slate from your account's "
                "third-party access settings, then connect again."
            )

    async def disconnect(self, identity: Identity, connection_id: UUID) -> None:
        """Revoke at the provider (best effort) and delete the connection: its tokens go
        with it. Only the person who connected it can. NotFound otherwise."""
        connection = await self._connections.get(
            identity.workspace_id, identity.user_id, connection_id
        )
        if connection is None:
            raise NotFound("This connection doesn't exist.")
        spec = self._spec(connection.provider)
        client = self._client(spec)
        tokens = await self._connections.tokens(connection.id)
        if spec.oauth is not None and client is not None and tokens is not None:
            await self._oauth.revoke(
                spec.oauth, client, tokens.refresh_token or tokens.access_token
            )
        await self._connections.delete(identity.workspace_id, identity.user_id, connection.id)
        log.info("integration.disconnected", provider=spec.id, connection_id=str(connection.id))

    # ------------------------------------------------------------ use

    async def access_token(self, connection_id: UUID) -> str:
        """A usable access token for this connection, refreshed if it is about to expire.

        Raises ReconnectRequired (the user must sign in again; the connection is marked)
        or UpstreamUnavailable (try later; the token is kept).
        """
        connection = await self._connections.by_id(connection_id)
        if connection is None:
            raise NotFound("This connection doesn't exist.")
        spec = self._spec(connection.provider)
        if connection.status is ConnectionStatus.NEEDS_RECONNECT:
            raise ReconnectRequired("Reconnect to continue.", connector=spec.id)
        tokens = await self._connections.tokens(connection.id)
        if tokens is None:
            await self._connections.mark(
                connection.id, ConnectionStatus.NEEDS_RECONNECT, "undecryptable"
            )
            raise ReconnectRequired("Reconnect to continue.", connector=spec.id)
        now = self._clock.now()
        if not needs_refresh(tokens.expires_at, now):
            return tokens.access_token
        client = self._client(spec)
        if spec.oauth is None or client is None or tokens.refresh_token is None:
            if tokens.expires_at is not None and tokens.expires_at > now:
                return tokens.access_token  # still valid, just can't be refreshed early
            await self._connections.mark(connection.id, ConnectionStatus.NEEDS_RECONNECT, "expired")
            raise ReconnectRequired("Access expired. Reconnect to continue.", connector=spec.id)
        if not await self._connections.try_lease(connection.id, now + REFRESH_LEASE):
            # Someone else is refreshing it right now.
            if tokens.expires_at is not None and tokens.expires_at > now:
                return tokens.access_token
            raise UpstreamUnavailable("Access is being refreshed. Try again in a moment.")
        try:
            fresh = await self._oauth.refresh(spec.oauth, client, tokens.refresh_token)
        except ReconnectRequired:
            await self._connections.mark(connection.id, ConnectionStatus.NEEDS_RECONNECT, "revoked")
            log.info(
                "integration.needs_reconnect", provider=spec.id, connection_id=str(connection.id)
            )
            raise
        except UpstreamUnavailable:
            await self._connections.mark(connection.id, connection.status, "refresh_failed")
            raise
        await self._connections.store_refresh(connection.id, fresh)
        return fresh.access_token

    # ------------------------------------------------------------ helpers

    def _spec(self, provider_id: str) -> ProviderSpec:
        spec = self._by_id.get(provider_id)
        if spec is None:
            raise NotFound("This integration doesn't exist.")
        return spec

    def _client(self, spec: ProviderSpec) -> ClientCredentials | None:
        return self._clients.get(spec.client) if spec.client else None

    def _connectable(
        self, identity: Identity, provider_id: str
    ) -> tuple[ProviderSpec, OAuthSpec, ClientCredentials, str]:
        """The spec and what connecting needs, or why this user can't connect it."""
        spec = self._spec(provider_id)
        if not identity.email_verified:
            raise PermissionDenied("Verify your email address first.")
        if identity.role is Role.VIEWER:
            raise PermissionDenied("Viewers can't connect accounts in this workspace.")
        client, oauth, redirect = self._client(spec), spec.oauth, self._redirect_uri
        if (
            spec.auth is not AuthKind.OAUTH2
            or not spec.available
            or oauth is None
            or client is None
            or redirect is None
            or not self._connections.available
        ):
            raise ValidationFailed(f"{spec.name} can't be connected on this server yet.")
        return spec, oauth, client, redirect
