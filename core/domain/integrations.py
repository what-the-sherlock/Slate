"""Integrations (ADR-0032): the provider registry and the rules of one OAuth 2.0 flow.

A provider is *data*: a ``ProviderSpec`` says how a user signs in (OAuth 2.0, or one of
the older install flows), which endpoints and scopes it uses, how it reports the
account, and whether Slate indexes it or queries it live. Adding a provider means adding
a spec and its adapter; the routes, the connect flow and the Integrations page are
shared. (Onyx's ``ProviderSpec``/``OAuthFlowSpec`` idea, adapted.)

OAuth rules, all pure:

* **State** is an HMAC-signed, 10-minute token bound to the user, the workspace and the
  provider (``core/domain/signed_tokens.py``). It travels through the provider's
  redirect and comes back in the callback body, where the session must match it.
* **PKCE** (RFC 7636, S256) for every provider that supports it. The verifier is never
  stored or sent anywhere but the token endpoint: it is *derived* from the state's nonce
  with a server key, so whoever sees the redirect URL (state and code) still can't
  redeem the code.
* **Refresh failures** are classed: *terminal* (the grant is dead: the user must
  reconnect) or *transient* (keep the token, try later).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal, cast
from urllib.parse import urlencode

from core.domain.errors import NotFound, ValidationFailed

STATE_PURPOSE = "integration-oauth"
STATE_TTL = timedelta(minutes=10)
# Refresh a little before the provider's expiry, so a token never dies mid-request.
REFRESH_SKEW = timedelta(minutes=5)
REFRESH_LEASE = timedelta(seconds=60)
MAX_LABEL_CHARS = 200
MAX_ERROR_CHARS = 300


class AuthKind(StrEnum):
    OAUTH2 = "oauth2"  # the generic flow below
    GITHUB_APP = "github_app"  # ADR-0023: install a GitHub App (its own flow)
    DISCORD_BOT = "discord_bot"  # ADR-0027: add the Slate bot (its own flow)
    APP_PASSWORD = "app_password"  # ADR-0028: IMAP with an app password


class Mode(StrEnum):
    INDEXED = "indexed"  # synced into Slate's index, searchable with citations
    LIVE = "live"  # read from the provider when a question needs it


class ConnectionStatus(StrEnum):
    """What a stored connection is (``connections.status``)."""

    CONNECTED = "connected"
    NEEDS_RECONNECT = "needs_reconnect"  # the grant was revoked or expired for good
    ERROR = "error"  # the last use failed for another reason (shown, retried)


class IntegrationState(StrEnum):
    """What the Integrations page shows for a provider, for this user."""

    CONNECTED = "connected"
    NEEDS_RECONNECT = "needs_reconnect"
    NOT_CONNECTED = "not_connected"
    NOT_SET_UP = "not_set_up"  # this server has no client id/secret for it
    COMING_SOON = "coming_soon"  # in the registry, adapter not built yet


@dataclass(frozen=True, slots=True)
class IdentitySpec:
    """Where the connected account's id and label come from: the token response itself
    (``source="token"``, e.g. Notion's workspace) or a JSON GET of ``url`` with the new
    token (``source="userinfo"``, e.g. Google). Fields are dotted paths."""

    source: Literal["token", "userinfo"]
    id_field: str
    label_field: str
    url: str | None = None


@dataclass(frozen=True, slots=True)
class OAuthSpec:
    authorize_url: str
    token_url: str
    identity: IdentitySpec
    scopes: tuple[str, ...] = ()
    scope_param: str = "scope"  # Slack asks for user scopes as "user_scope"
    scope_separator: str = " "
    extra_params: Mapping[str, str] = field(default_factory=dict[str, str])
    pkce: bool = True
    client_auth: Literal["body", "basic"] = "body"  # Notion wants HTTP Basic
    token_body: Literal["form", "json"] = "form"  # Notion wants JSON
    revoke_url: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    id: str
    name: str
    tagline: str
    auth: AuthKind
    mode: Mode
    # Which client id/secret pair this provider uses (Gmail, Calendar and Drive share
    # one Google client). Only for OAUTH2.
    client: str | None = None
    oauth: OAuthSpec | None = None
    # Whether this build can connect it (its adapter exists). False: "Coming soon".
    available: bool = False
    # Where the older install flows live in the web app (GitHub, Discord, email).
    connect_path: str | None = None
    # Shown before the user is sent to the provider (e.g. Google's unverified-app notice).
    notice: str | None = None
    # The connector names of the sources this provider creates, for older flows' status.
    connectors: tuple[str, ...] = ()


GOOGLE_UNVERIFIED_NOTICE = (
    'Google will show an "unverified app" notice. Reading mail, calendars and files '
    "uses Google scopes whose verification requires a paid third-party security "
    "assessment, which this portfolio deployment hasn't gone through. Slate's access is "
    "read-only, tokens are encrypted at rest, and you can revoke access at any time from "
    "your Google account."
)

_GOOGLE_IDENTITY = IdentitySpec(
    "userinfo", "sub", "email", url="https://openidconnect.googleapis.com/v1/userinfo"
)


def _google(scope: str) -> OAuthSpec:
    return OAuthSpec(
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        identity=_GOOGLE_IDENTITY,
        scopes=("openid", "email", scope),
        # offline: a refresh token; consent: Google re-issues it on every reconnect.
        extra_params={"access_type": "offline", "prompt": "consent"},
        revoke_url="https://oauth2.googleapis.com/revoke",
    )


# The order is the page's order: what's connectable first, then by usefulness.
REGISTRY: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        "github", "GitHub", "Docs, issues and pull requests from your repositories.",
        AuthKind.GITHUB_APP, Mode.INDEXED, available=True,
        connect_path="/sources?add=github", connectors=("github",),
    ),
    ProviderSpec(
        "discord", "Discord", "Conversations from the channels you choose.",
        AuthKind.DISCORD_BOT, Mode.INDEXED, available=True,
        connect_path="/sources?add=discord", connectors=("discord",),
    ),
    ProviderSpec(
        "email", "Email (IMAP)", "Gmail, Yahoo, iCloud or Fastmail with an app password.",
        AuthKind.APP_PASSWORD, Mode.INDEXED, available=True,
        connect_path="/sources?add=email", connectors=("imap",),
    ),
    ProviderSpec(
        "gmail", "Gmail", "Sign in with Google to search your mail, read-only.",
        AuthKind.OAUTH2, Mode.INDEXED, client="google",
        oauth=_google("https://www.googleapis.com/auth/gmail.readonly"),
        notice=GOOGLE_UNVERIFIED_NOTICE,
    ),
    ProviderSpec(
        "notion", "Notion", "Pages and databases you share with Slate.",
        AuthKind.OAUTH2, Mode.INDEXED, client="notion",
        oauth=OAuthSpec(
            authorize_url="https://api.notion.com/v1/oauth/authorize",
            token_url="https://api.notion.com/v1/oauth/token",
            identity=IdentitySpec("token", "workspace_id", "workspace_name"),
            extra_params={"owner": "user", "response_type": "code"},
            pkce=False,
            client_auth="basic",
            token_body="json",
        ),
    ),
    ProviderSpec(
        "linear", "Linear", "Issues and projects from your Linear workspace.",
        AuthKind.OAUTH2, Mode.INDEXED, client="linear",
        oauth=OAuthSpec(
            authorize_url="https://linear.app/oauth/authorize",
            token_url="https://api.linear.app/oauth/token",
            identity=IdentitySpec("token", "organization.id", "organization.name"),
            scopes=("read",),
            scope_separator=",",
            extra_params={"actor": "user"},
            revoke_url="https://api.linear.app/oauth/revoke",
        ),
    ),
    ProviderSpec(
        "google_calendar", "Google Calendar", "Your upcoming events, looked up live.",
        AuthKind.OAUTH2, Mode.LIVE, client="google",
        oauth=_google("https://www.googleapis.com/auth/calendar.readonly"),
        notice=GOOGLE_UNVERIFIED_NOTICE,
    ),
    ProviderSpec(
        "google_drive", "Google Drive", "Files you pick from your Drive.",
        AuthKind.OAUTH2, Mode.INDEXED, client="google",
        # drive.file: only files the user picks, a non-restricted scope (ADR-0032).
        oauth=_google("https://www.googleapis.com/auth/drive.file"),
        notice=GOOGLE_UNVERIFIED_NOTICE,
    ),
    ProviderSpec(
        "slack", "Slack", "Messages from channels you can read.",
        AuthKind.OAUTH2, Mode.INDEXED, client="slack",
        oauth=OAuthSpec(
            authorize_url="https://slack.com/oauth/v2/authorize",
            token_url="https://slack.com/api/oauth.v2.access",
            identity=IdentitySpec("token", "team.id", "team.name"),
            scopes=("channels:history", "channels:read", "users:read"),
            scope_param="user_scope",
            scope_separator=",",
            pkce=False,
        ),
    ),
    ProviderSpec(
        "hubspot", "HubSpot", "Contacts, companies and deals, looked up live.",
        AuthKind.OAUTH2, Mode.LIVE, client="hubspot",
        oauth=OAuthSpec(
            authorize_url="https://app.hubspot.com/oauth/authorize",
            token_url="https://api.hubapi.com/oauth/v1/token",
            identity=IdentitySpec("token", "hub_id", "hub_domain"),
            scopes=(
                "oauth",
                "crm.objects.contacts.read",
                "crm.objects.companies.read",
                "crm.objects.deals.read",
            ),
            pkce=False,
        ),
    ),
)  # fmt: skip

_BY_ID = {p.id: p for p in REGISTRY}


def provider(provider_id: str) -> ProviderSpec:
    """The spec, or NotFound for an unknown id (never echoes the id into SQL or URLs)."""
    spec = _BY_ID.get(provider_id)
    if spec is None:
        raise NotFound("This integration doesn't exist.")
    return spec


# ---------------------------------------------------------------- PKCE and state


def new_nonce() -> str:
    return secrets.token_urlsafe(24)


def derive_verifier(key: bytes, nonce: str) -> str:
    """The PKCE code verifier for this attempt: 43 base64url characters (RFC 7636 §4.1),
    reproducible only with the server key, so it is never stored or put in a URL."""
    mac = hmac.new(key, b"pkce\x00" + nonce.encode(), hashlib.sha256).digest()
    return _b64(mac)


def code_challenge(verifier: str) -> str:
    """S256: base64url(SHA-256(verifier)), no padding (RFC 7636 §4.2)."""
    return _b64(hashlib.sha256(verifier.encode()).digest())


def authorize_url(
    spec: OAuthSpec, *, client_id: str, redirect_uri: str, state: str, challenge: str | None
) -> str:
    params: dict[str, str] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "state": state,
    }
    if spec.scopes:
        params[spec.scope_param] = spec.scope_separator.join(spec.scopes)
    if spec.pkce and challenge:
        params["code_challenge"] = challenge
        params["code_challenge_method"] = "S256"
    params.update(spec.extra_params)
    return f"{spec.authorize_url}?{urlencode(params)}"


# ---------------------------------------------------------------- tokens


@dataclass(frozen=True, slots=True)
class TokenSet:
    access_token: str
    refresh_token: str | None = None
    expires_at: datetime | None = None
    scopes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Account:
    """The provider account a connection signs in as."""

    id: str
    label: str | None


class RefreshFailure(StrEnum):
    TERMINAL = "terminal"
    TRANSIENT = "transient"


# OAuth error codes that mean the grant can never work again (RFC 6749 §5.2), plus
# Slack's equivalents. Anything else is treated as transient.
_TERMINAL_CODES = {
    "invalid_grant",
    "invalid_client",
    "unauthorized_client",
    "access_denied",
    "invalid_refresh_token",
    "token_revoked",
    "invalid_auth",
    "account_inactive",
}


def classify_failure(status: int | None, body: object) -> RefreshFailure:
    """Terminal when the provider names a dead grant (or answers 400/401 with no
    code); transient for network errors (``status`` None), 429, 5xx and anything else."""
    code = _error_code(body)
    if code in _TERMINAL_CODES:
        return RefreshFailure.TERMINAL
    if status is None or status == 429 or status >= 500:
        return RefreshFailure.TRANSIENT
    if status in (400, 401) and code is None:
        return RefreshFailure.TERMINAL
    return RefreshFailure.TRANSIENT


def token_error(status: int, body: object) -> str | None:
    """The provider's error code, or None on success. Slack answers 200 with
    ``{"ok": false}``; everyone else uses a 4xx/5xx."""
    if status >= 400:
        return _error_code(body) or f"http_{status}"
    if _obj(body).get("ok") is False:
        return _error_code(body) or "unknown"
    return None


def parse_tokens(body: object, now: datetime, *, keep_refresh: str | None = None) -> TokenSet:
    """A token response as a TokenSet. Slack nests user tokens under ``authed_user``.
    ``keep_refresh``: providers that don't rotate refresh tokens omit them on refresh,
    so the old one is kept. Raises ValidationFailed without an access token."""
    data = _obj(body)
    if isinstance(data.get("authed_user"), dict) and "access_token" not in data:
        data = {**data, **_obj(data["authed_user"])}
    access = data.get("access_token")
    if not isinstance(access, str) or not access:
        raise ValidationFailed("The provider didn't return an access token.")
    refresh = data.get("refresh_token")
    expires_in = data.get("expires_in")
    expires = (
        now + timedelta(seconds=float(expires_in))
        if isinstance(expires_in, int | float) and not isinstance(expires_in, bool)
        else None
    )
    scope = data.get("scope")
    scopes = tuple(scope.replace(",", " ").split()) if isinstance(scope, str) else ()
    return TokenSet(
        access_token=access,
        refresh_token=refresh if isinstance(refresh, str) and refresh else keep_refresh,
        expires_at=expires,
        scopes=scopes,
    )


def account_from(identity: IdentitySpec, body: object) -> Account:
    """The account named by ``identity`` in a token or userinfo response. Raises
    ValidationFailed when the id is missing (the connection would be unnamed)."""
    data = _obj(body)
    if isinstance(data.get("authed_user"), dict):
        data = {**_obj(data["authed_user"]), **data}
    raw_id = _path(data, identity.id_field)
    if raw_id is None or str(raw_id).strip() == "":
        raise ValidationFailed("The provider didn't say which account was connected.")
    label = _path(data, identity.label_field)
    text = " ".join(str(label).split())[:MAX_LABEL_CHARS] if label is not None else None
    return Account(id=str(raw_id)[:MAX_LABEL_CHARS], label=text or None)


def needs_refresh(expires_at: datetime | None, now: datetime) -> bool:
    return expires_at is not None and expires_at - REFRESH_SKEW <= now


def secret_json(tokens: TokenSet) -> str:
    """What is encrypted into ``connections.secret_encrypted``."""
    return json.dumps(
        {
            "a": tokens.access_token,
            "r": tokens.refresh_token,
            "e": tokens.expires_at.isoformat() if tokens.expires_at else None,
            "s": list(tokens.scopes),
        },
        separators=(",", ":"),
    )


def tokens_from_secret(secret: str) -> TokenSet | None:
    """The stored tokens, or None if the secret is unreadable (treat as reconnect)."""
    try:
        data = _obj(json.loads(secret))
    except ValueError:
        return None
    access, refresh, exp = data.get("a"), data.get("r"), data.get("e")
    if not isinstance(access, str) or not access:
        return None
    try:
        expires = datetime.fromisoformat(exp) if isinstance(exp, str) else None
    except ValueError:
        expires = None
    scopes = data.get("s")
    return TokenSet(
        access_token=access,
        refresh_token=refresh if isinstance(refresh, str) else None,
        expires_at=expires,
        scopes=tuple(str(s) for s in cast(list[object], scopes))
        if isinstance(scopes, list)
        else (),
    )


def short_error(code: str) -> str:
    """A stored, user-visible error: the provider's code only, never a token or body."""
    safe = "".join(ch for ch in code if ch.isalnum() or ch in "_-. ")
    return safe[:MAX_ERROR_CHARS] or "unknown"


def connection_state(statuses: Sequence[ConnectionStatus]) -> IntegrationState:
    """One provider's state from this user's connections to it: any that needs a
    reconnect wins, so the page asks for it."""
    if any(s is ConnectionStatus.NEEDS_RECONNECT for s in statuses):
        return IntegrationState.NEEDS_RECONNECT
    if statuses:
        return IntegrationState.CONNECTED
    return IntegrationState.NOT_CONNECTED


def _error_code(body: object) -> str | None:
    if not isinstance(body, dict):
        return None
    err = cast(dict[str, object], body).get("error")
    return err if isinstance(err, str) and err else None


def _obj(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict):
        return {}
    return {str(k): v for k, v in cast(dict[object, object], raw).items()}


def _path(data: Mapping[str, object], dotted: str) -> object:
    cur: object = data
    for part in dotted.split("."):
        here = _obj(cur)
        if not here:
            return None
        cur = here.get(part)
    return cur


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()
