"""OAuthProvider over httpx (ADR-0032): one client for every provider's spec.

* Only the spec's fixed HTTPS endpoints are called (never a URL from a response), with no
  redirects, so a token or client secret can only reach the provider that issued it.
* The code exchange and refreshes are never retried: both are single use (refresh tokens
  rotate), and a retry after a lost response could kill the grant.
* Failures are classed by ``classify_failure``: a dead grant raises ReconnectRequired,
  anything else UpstreamUnavailable. Only the provider's error *code* is logged or kept,
  never a response body (it can hold tokens).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx
import structlog

from core.domain.errors import ReconnectRequired, UpstreamUnavailable, ValidationFailed
from core.domain.integrations import (
    Account,
    OAuthSpec,
    RefreshFailure,
    TokenSet,
    account_from,
    classify_failure,
    parse_tokens,
    token_error,
)
from core.ports.integrations import ClientCredentials

log = structlog.get_logger(__name__)

TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0)


class HttpOAuthProvider:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    async def exchange(
        self,
        spec: OAuthSpec,
        client: ClientCredentials,
        code: str,
        verifier: str | None,
        redirect_uri: str,
    ) -> tuple[TokenSet, Account]:
        grant: dict[str, str] = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        }
        if verifier is not None:
            grant["code_verifier"] = verifier
        status, body = await self._token_call(spec, client, grant)
        if (err := token_error(status, body)) is not None:
            log.info("oauth.exchange_refused", host=_host(spec.token_url), error=err)
            raise ValidationFailed(
                "The sign-in didn't complete (the provider refused the code). Try again."
            )
        tokens = parse_tokens(body, datetime.now(UTC))
        identity = spec.identity
        if identity.source == "token":
            account = account_from(identity, body)
        else:
            account = account_from(identity, await self._userinfo(spec, tokens.access_token))
        return tokens, account

    async def refresh(
        self, spec: OAuthSpec, client: ClientCredentials, refresh_token: str
    ) -> TokenSet:
        grant = {"grant_type": "refresh_token", "refresh_token": refresh_token}
        # Network trouble raises UpstreamUnavailable from _post: transient, the token stays.
        status, body = await self._token_call(spec, client, grant)
        if (err := token_error(status, body)) is not None:
            failure = classify_failure(status, body)
            log.info("oauth.refresh_failed", host=_host(spec.token_url), error=err, kind=failure)
            if failure is RefreshFailure.TERMINAL:
                raise ReconnectRequired(
                    "Access was revoked or expired. Reconnect to continue.",
                    connector=_host(spec.token_url),
                )
            raise UpstreamUnavailable("The provider couldn't refresh access right now.")
        return parse_tokens(body, datetime.now(UTC), keep_refresh=refresh_token)

    async def revoke(self, spec: OAuthSpec, client: ClientCredentials, token: str) -> None:
        if spec.revoke_url is None:
            return
        # Google takes the token as a form field, Linear as a bearer token: send both.
        headers = {"Authorization": f"Bearer {token}"}
        try:
            await self._post(spec.revoke_url, data={"token": token}, headers=headers)
        except UpstreamUnavailable:
            log.info("oauth.revoke_failed", host=_host(spec.revoke_url))

    # ------------------------------------------------------------------ HTTP

    async def _token_call(
        self, spec: OAuthSpec, client: ClientCredentials, grant: dict[str, str]
    ) -> tuple[int, object]:
        body = dict(grant)
        auth: tuple[str, str] | None = None
        if spec.client_auth == "basic":
            auth = _basic(client)
        else:
            body |= {"client_id": client.client_id, "client_secret": client.client_secret}
        headers = {"Accept": "application/json"}
        if spec.token_body == "json":  # noqa: S105 (an encoding name, not a secret)
            response = await self._post(spec.token_url, json=body, auth=auth, headers=headers)
        else:
            response = await self._post(spec.token_url, data=body, auth=auth, headers=headers)
        return response.status_code, _json(response)

    async def _userinfo(self, spec: OAuthSpec, access_token: str) -> object:
        url = spec.identity.url
        if url is None:
            raise ValidationFailed("This provider has no account endpoint configured.")
        _check_https(url)
        headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
        try:
            response = await self._request("GET", url, headers=headers)
        except httpx.HTTPError as e:
            raise UpstreamUnavailable("The provider didn't answer. Try again.") from e
        if response.status_code != 200:
            log.info("oauth.userinfo_failed", host=_host(url), status=response.status_code)
            raise UpstreamUnavailable("The provider didn't say which account signed in.")
        return _json(response)

    async def _post(self, url: str, **kw: Any) -> httpx.Response:
        _check_https(url)
        try:
            return await self._request("POST", url, **kw)
        except httpx.HTTPError as e:
            log.info("oauth.unreachable", host=_host(url), error=type(e).__name__)
            raise UpstreamUnavailable("The provider didn't answer. Try again.") from e

    async def _request(self, method: str, url: str, **kw: Any) -> httpx.Response:
        if self._client is not None:
            return await self._client.request(method, url, timeout=TIMEOUT, **kw)
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as c:
            return await c.request(method, url, **kw)


def _basic(client: ClientCredentials) -> tuple[str, str]:
    return (client.client_id, client.client_secret)


def _json(response: httpx.Response) -> object:
    try:
        return response.json()
    except ValueError:
        return None


def _check_https(url: str) -> None:
    if urlparse(url).scheme != "https":
        raise ValueError("OAuth endpoints must be https")


def _host(url: str) -> str:
    return urlparse(url).hostname or "?"
