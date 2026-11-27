from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pytest

from core.domain.auth import Identity, Role
from core.domain.data_class import DataClass
from core.domain.errors import (
    NotFound,
    PermissionDenied,
    RateLimited,
    ReconnectRequired,
    UpstreamUnavailable,
    ValidationFailed,
)
from core.domain.ids import SessionId, UserId, WorkspaceId
from core.domain.integrations import (
    REGISTRY,
    ConnectionStatus,
    IntegrationState,
    TokenSet,
    code_challenge,
    provider,
)
from core.domain.principals import Principals
from core.ports.integrations import ClientCredentials, Connection
from core.services.integrations import CONNECT_LIMIT, IntegrationService, IntegrationView
from tests.fakes.integrations import FakeOAuth, InMemoryConnections, terminal, transient
from tests.fakes.rate_limit import InMemoryRateLimiter
from tests.fakes.sources import InMemorySources
from tests.fakes.system import FakeClock

WS = WorkspaceId(uuid4())
OTHER_WS = WorkspaceId(uuid4())
ADA = UserId(uuid4())
BOB = UserId(uuid4())
NOW = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)
KEY = b"k" * 32
REDIRECT = "https://slate.test/integrations/callback"
# Linear made connectable, as a provider becomes once its adapter exists (M10).
LINEAR = replace(provider("linear"), available=True)
NOTION = replace(provider("notion"), available=True)
REG = tuple(LINEAR if p.id == "linear" else NOTION if p.id == "notion" else p for p in REGISTRY)


def who(
    user: UserId = ADA, ws: WorkspaceId = WS, *, role: Role = Role.MEMBER, verified: bool = True
) -> Identity:
    return Identity(
        user_id=user, workspace_id=ws, session_id=SessionId(uuid4()), email="a@b.c", name="A",
        role=role, email_verified=verified, principals=Principals.for_user(user, ws),
        csrf_secret="c",
    )  # fmt: skip


class Env:
    def __init__(self, *, clients: dict[str, ClientCredentials] | None = None) -> None:
        self.clock = FakeClock(NOW)
        self.store = InMemoryConnections(now=NOW)
        self.oauth = FakeOAuth()
        self.sources = InMemorySources()
        self.svc = IntegrationService(
            self.store,
            self.oauth,
            self.sources,
            InMemoryRateLimiter(self.clock),
            self.clock,
            secret=KEY,
            clients=clients if clients is not None else {
                "linear": ClientCredentials("lin-id", "lin-secret"),
                "notion": ClientCredentials("not-id", "not-secret"),
            },
            redirect_uri=REDIRECT,
            legacy_ready={"github": True, "discord": False, "email": True},
            registry=REG,
        )  # fmt: skip

    async def connect(
        self, user: Identity | None = None, provider_id: str = "linear"
    ) -> Connection:
        user = user or who()
        url = await self.svc.start(user, provider_id)
        state = parse_qs(urlparse(url).query)["state"][0]
        return await self.svc.complete(user, provider_id, "the-code", state)


def states(views: Sequence[IntegrationView]) -> dict[str, IntegrationState]:
    return {v.spec.id: v.state for v in views}


# ---------------------------------------------------------------- catalogue


async def test_catalogue_states_reflect_setup_availability_and_connections() -> None:
    env = Env()
    await env.sources.create_source(
        WS, connector="github", name="repo", config={}, visibility=[f"user:{ADA}"],
        data_class=DataClass.PRIVATE, created_by=ADA,
    )  # fmt: skip
    got = states(await env.svc.catalog(who()))
    assert got["github"] is IntegrationState.CONNECTED  # an older-flow source of Ada's
    assert got["discord"] is IntegrationState.NOT_SET_UP  # its flow isn't configured here
    assert got["email"] is IntegrationState.NOT_CONNECTED
    assert got["linear"] is IntegrationState.NOT_CONNECTED
    assert got["gmail"] is IntegrationState.COMING_SOON  # no adapter in this build
    assert list(got) == [p.id for p in REGISTRY]  # the registry's order


async def test_an_available_provider_without_a_client_is_not_set_up() -> None:
    env = Env(clients={})
    assert states(await env.svc.catalog(who()))["linear"] is IntegrationState.NOT_SET_UP


async def test_someone_elses_sources_and_connections_never_count() -> None:
    env = Env()
    await env.sources.create_source(
        WS, connector="github", name="repo", config={}, visibility=[f"user:{BOB}"],
        data_class=DataClass.PRIVATE, created_by=BOB,
    )  # fmt: skip
    await env.connect(who(BOB))
    got = states(await env.svc.catalog(who()))
    assert got["github"] is IntegrationState.NOT_CONNECTED
    assert got["linear"] is IntegrationState.NOT_CONNECTED


# ---------------------------------------------------------------- connect


async def test_start_signs_state_and_sends_a_pkce_challenge() -> None:
    env = Env()
    url = await env.svc.start(who(), "linear")
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert q["client_id"] == "lin-id"
    assert q["redirect_uri"] == REDIRECT
    assert q["code_challenge_method"] == "S256"
    assert "lin-secret" not in url


async def test_complete_redeems_the_code_with_the_derived_verifier_and_stores_it() -> None:
    env = Env()
    url = await env.svc.start(who(), "linear")
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    conn = await env.svc.complete(who(), "linear", "the-code", q["state"])
    [(code, verifier, redirect)] = env.oauth.exchanged
    assert (code, redirect) == ("the-code", REDIRECT)
    assert verifier is not None
    assert code_challenge(verifier) == q["code_challenge"]
    assert conn.status is ConnectionStatus.CONNECTED
    assert conn.account_label == "ada@example.test"
    assert states(await env.svc.catalog(who()))["linear"] is IntegrationState.CONNECTED


async def test_a_provider_without_pkce_gets_no_verifier() -> None:
    env = Env()
    await env.connect(provider_id="notion")
    assert env.oauth.exchanged[0][1] is None


@pytest.mark.parametrize(
    "thief",
    [who(BOB), who(ADA, OTHER_WS)],
    ids=["another user", "another workspace"],
)
async def test_state_is_bound_to_the_user_and_workspace(thief: Identity) -> None:
    env = Env()
    url = await env.svc.start(who(), "linear")
    state = parse_qs(urlparse(url).query)["state"][0]
    with pytest.raises(ValidationFailed, match="expired or isn't yours"):
        await env.svc.complete(thief, "linear", "code", state)
    assert env.oauth.exchanged == []


async def test_state_is_bound_to_the_provider_and_expires() -> None:
    env = Env()
    state = parse_qs(urlparse(await env.svc.start(who(), "linear")).query)["state"][0]
    with pytest.raises(ValidationFailed):
        await env.svc.complete(who(), "notion", "code", state)
    env.clock._now = NOW + timedelta(minutes=11)
    with pytest.raises(ValidationFailed):
        await env.svc.complete(who(), "linear", "code", state)
    with pytest.raises(ValidationFailed):
        await env.svc.complete(who(), "linear", "code", "forged.state")
    assert env.oauth.exchanged == []


async def test_reconnecting_the_same_account_replaces_it_not_duplicates() -> None:
    env = Env()
    await env.connect()
    await env.connect()
    assert len(await env.store.list_for_user(WS, ADA)) == 1


@pytest.mark.parametrize(
    ("identity", "provider_id", "error"),
    [
        (who(role=Role.VIEWER), "linear", PermissionDenied),
        (who(verified=False), "linear", PermissionDenied),
        (who(), "gmail", ValidationFailed),  # coming soon
        (who(), "github", ValidationFailed),  # not an OAuth flow
        (who(), "nope", NotFound),
    ],
)
async def test_who_and_what_can_be_connected(
    identity: Identity, provider_id: str, error: type[Exception]
) -> None:
    with pytest.raises(error):
        await Env().svc.start(identity, provider_id)


async def test_connecting_is_rate_limited() -> None:
    env = Env()
    for _ in range(CONNECT_LIMIT.budget):
        await env.svc.start(who(), "linear")
    with pytest.raises(RateLimited):
        await env.svc.start(who(), "linear")


async def test_without_an_encryption_key_nothing_can_be_connected() -> None:
    env = Env()
    env.store.available = False
    with pytest.raises(ValidationFailed):
        await env.svc.start(who(), "linear")


# ---------------------------------------------------------------- disconnect


async def test_disconnect_revokes_and_deletes_only_your_own() -> None:
    env = Env()
    conn = await env.connect()
    with pytest.raises(NotFound):
        await env.svc.disconnect(who(BOB), conn.id)
    await env.svc.disconnect(who(), conn.id)
    assert env.oauth.revoked == ["refresh-1"]
    assert await env.store.list_for_user(WS, ADA) == []


# ---------------------------------------------------------------- access tokens


async def connected(env: Env, expires_in: timedelta | None) -> Connection:
    env.oauth.tokens = TokenSet("access-1", "refresh-1", NOW + expires_in if expires_in else None)
    return await env.connect()


async def test_a_fresh_token_is_returned_without_refreshing() -> None:
    env = Env()
    conn = await connected(env, timedelta(hours=1))
    assert await env.svc.access_token(conn.id) == "access-1"
    assert env.oauth.refreshed == []


async def test_a_token_about_to_expire_is_refreshed_and_stored() -> None:
    env = Env()
    conn = await connected(env, timedelta(minutes=2))
    assert await env.svc.access_token(conn.id) == "access-2"
    assert env.oauth.refreshed == ["refresh-1"]
    stored = await env.store.tokens(conn.id)
    assert stored is not None
    assert stored.refresh_token == "refresh-2"  # the rotated refresh token is kept


async def test_a_dead_grant_marks_the_connection_for_reconnect() -> None:
    env = Env()
    conn = await connected(env, timedelta(minutes=2))
    env.oauth.refresh_outcome = terminal()
    with pytest.raises(ReconnectRequired):
        await env.svc.access_token(conn.id)
    row = await env.store.by_id(conn.id)
    assert row is not None
    assert row.status is ConnectionStatus.NEEDS_RECONNECT
    assert states(await env.svc.catalog(who()))["linear"] is IntegrationState.NEEDS_RECONNECT
    with pytest.raises(ReconnectRequired):  # and it isn't retried at the provider
        await env.svc.access_token(conn.id)
    assert len(env.oauth.refreshed) == 1


async def test_a_transient_failure_keeps_the_token_and_the_status() -> None:
    env = Env()
    conn = await connected(env, timedelta(minutes=2))
    env.oauth.refresh_outcome = transient()
    with pytest.raises(UpstreamUnavailable):
        await env.svc.access_token(conn.id)
    row = await env.store.by_id(conn.id)
    assert row is not None
    assert row.status is ConnectionStatus.CONNECTED
    tokens = await env.store.tokens(conn.id)
    assert tokens is not None
    assert tokens.access_token == "access-1"


async def test_while_another_refresh_runs_a_still_valid_token_is_used() -> None:
    env = Env()
    conn = await connected(env, timedelta(minutes=2))
    env.store.leases[conn.id] = NOW + timedelta(seconds=60)
    assert await env.svc.access_token(conn.id) == "access-1"
    assert env.oauth.refreshed == []


async def test_an_expired_token_without_a_refresh_token_needs_reconnect() -> None:
    env = Env()
    env.oauth.tokens = TokenSet("access-1", None, NOW - timedelta(minutes=1))
    conn = await env.connect()
    with pytest.raises(ReconnectRequired):
        await env.svc.access_token(conn.id)


async def test_an_undecryptable_secret_needs_reconnect() -> None:
    env = Env()
    conn = await connected(env, None)
    env.store.secrets.pop(conn.id)
    with pytest.raises(ReconnectRequired):
        await env.svc.access_token(conn.id)
