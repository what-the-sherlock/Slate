"""Calls to a provider's data API with a user's access token (ADR-0033), shared by every
connected-source adapter so they fail the same way:

* 401 (the token was refused) -> ReconnectRequired: the user must sign in again.
* 429, or 403 that says "rate limit" -> RateLimited with the provider's Retry-After.
* other 403 -> ConnectorError (e.g. the API isn't enabled, a scope is missing), not retried.
* 5xx and network errors are retried twice with backoff, then UpstreamUnavailable.
* 404 -> None for callers that allow it (an item deleted between list and fetch).

Only fixed https hosts per adapter are called, redirects are never followed, and only
status codes and the provider's error reason are logged (never a body: it holds user data).
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Mapping
from typing import Any, cast
from urllib.parse import urlparse

import httpx
import structlog

from core.domain.errors import ConnectorError, RateLimited, ReconnectRequired, UpstreamUnavailable

log = structlog.get_logger(__name__)

TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0)
ATTEMPTS = 3
DEFAULT_RETRY_S = 60
MAX_RETRY_S = 3600
_RATE_REASONS = ("ratelimit", "rate_limit", "userratelimit", "quotaexceeded", "ratelimited")


class ProviderApi:
    def __init__(
        self,
        name: str,
        hosts: frozenset[str],
        client: httpx.AsyncClient | None = None,
        *,
        sleep: float = 1.0,
    ) -> None:
        self.name = name
        self._hosts = hosts
        self._client = client
        self._sleep = sleep  # backoff base; tests pass 0

    async def get(
        self,
        token: str,
        url: str,
        *,
        params: Any = None,
        allow_404: bool = False,
        headers: Mapping[str, str] | None = None,
    ) -> Any:  # reason: JSON bodies are Any
        r = await self._send("GET", token, url, params=params, headers=dict(headers or {}))
        return self._result(r, url, allow_404)

    async def post(
        self,
        token: str,
        url: str,
        *,
        json: Any,
        allow_404: bool = False,
        headers: Mapping[str, str] | None = None,
    ) -> Any:  # reason: JSON bodies are Any
        r = await self._send("POST", token, url, json=json, headers=dict(headers or {}))
        return self._result(r, url, allow_404)

    async def get_text(
        self, token: str, url: str, *, params: Any = None, max_bytes: int, allow_404: bool = False
    ) -> str | None:
        """A text body (a document export), capped; None for 404 when allowed."""
        r = await self._send("GET", token, url, params=params, headers={})
        if r.status_code == 404 and allow_404:
            return None
        self._raise(r, url)
        return r.content[:max_bytes].decode("utf-8", errors="replace")

    def _result(self, r: httpx.Response, url: str, allow_404: bool) -> Any:
        if r.status_code == 404 and allow_404:
            return None
        self._raise(r, url)
        try:
            return r.json()
        except ValueError as e:
            raise UpstreamUnavailable(f"{self.name} sent an unreadable reply.") from e

    async def _send(
        self, method: str, token: str, url: str, *, headers: dict[str, str], **kw: Any
    ) -> httpx.Response:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in self._hosts:
            raise ValueError(f"{self.name}: refusing to call {parsed.hostname}")
        all_headers = {"Authorization": f"Bearer {token}", "Accept": "application/json", **headers}
        for attempt in range(ATTEMPTS):
            try:
                r = await self._request(method, url, headers=all_headers, **kw)
            except httpx.HTTPError as e:
                log.info("provider.unreachable", provider=self.name, error=type(e).__name__)
                if attempt == ATTEMPTS - 1:
                    raise UpstreamUnavailable(f"{self.name} didn't answer. Try again later.") from e
            else:
                if r.status_code < 500 or attempt == ATTEMPTS - 1:
                    return r
            await asyncio.sleep(self._sleep * (2**attempt) * random.uniform(0.5, 1.0))  # noqa: S311
        raise AssertionError("unreachable")  # pragma: no cover

    def _raise(self, r: httpx.Response, url: str) -> None:
        status = r.status_code
        if status < 400:
            return
        reason = _reason(r)
        log.info("provider.error", provider=self.name, status=status, reason=reason)
        if status == 401:
            raise ReconnectRequired(
                f"{self.name} no longer accepts Slate's access.", connector=self.name
            )
        if status == 429 or (status == 403 and any(k in reason for k in _RATE_REASONS)):
            raise RateLimited(
                f"{self.name}'s rate limit was reached; syncing resumes later.",
                retry_after_s=_retry_after(r),
            )
        if status == 403:
            raise ConnectorError(
                f"{self.name} refused access ({reason or 'forbidden'}). Reconnect and allow "
                "every permission Slate asks for.",
                retryable=False,
                connector=self.name,
            )
        if status >= 500:
            raise UpstreamUnavailable(f"{self.name} is having trouble. Syncing resumes later.")
        raise ConnectorError(
            f"{self.name} rejected a request ({status}).", retryable=False, connector=self.name
        )

    async def _request(self, method: str, url: str, **kw: Any) -> httpx.Response:
        if self._client is not None:
            return await self._client.request(method, url, timeout=TIMEOUT, **kw)
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as c:
            return await c.request(method, url, **kw)


def _reason(r: httpx.Response) -> str:
    """The provider's machine-readable error reason, lowercased (never the message text)."""
    try:
        body: object = r.json()
    except ValueError:
        return ""
    data = _dict(body)
    err = data.get("error")
    if isinstance(err, str):  # Slack, OAuth-style
        return err.lower()
    detail = _dict(err)
    if detail:  # Google: {"error": {"status": ..., "errors": [{"reason": ...}]}}
        errors = detail.get("errors")
        first = _dict(errors[0]) if isinstance(errors, list) and errors else {}  # pyright: ignore[reportUnknownArgumentType]
        return str(first.get("reason") or detail.get("status") or "").lower()
    code = data.get("code")  # Notion
    return code.lower() if isinstance(code, str) else ""


def _dict(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict):
        return {}
    return {str(k): v for k, v in cast(dict[object, object], raw).items()}


def _retry_after(r: httpx.Response) -> int:
    raw = r.headers.get("retry-after", "")
    try:
        return max(1, min(int(float(raw)), MAX_RETRY_S))
    except ValueError:
        return DEFAULT_RETRY_S
