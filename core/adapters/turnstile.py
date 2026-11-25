"""HumanVerifier with Cloudflare Turnstile Siteverify (free, no card).

Plain httpx. Explicit timeouts. A Turnstile token is single use, so a blind retry would
fail with ``timeout-or-duplicate``; Siteverify accepts an ``idempotency_key`` that makes
the retry safe, so we send one and retry once on network errors. If verification can't be
done, raise UpstreamUnavailable and the caller fails closed. The secret is sent only in
the form body and never logged.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import structlog

from core.domain.errors import UpstreamUnavailable

log = structlog.get_logger(__name__)
SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
TIMEOUT = httpx.Timeout(5.0, connect=3.0)
MAX_TOKEN_LENGTH = 2048  # Cloudflare's documented limit


class TurnstileVerifier:
    def __init__(self, secret: str, *, client: httpx.AsyncClient | None = None) -> None:
        self._secret = secret
        self._client = client

    async def verify(self, token: str, ip: str | None) -> bool:
        if not token or len(token) > MAX_TOKEN_LENGTH:
            return False
        form = {"secret": self._secret, "response": token, "idempotency_key": str(uuid.uuid4())}
        if ip:
            form["remoteip"] = ip
        body = await self._post(form)
        ok = body.get("success") is True
        if not ok:
            codes: Any = body.get("error-codes", [])
            log.info("turnstile.rejected", error_codes=[str(c) for c in codes][:5])
        return ok

    async def _post(self, form: dict[str, str]) -> dict[str, Any]:
        last: Exception | None = None
        for _ in range(2):  # same idempotency key: safe to repeat
            try:
                if self._client is not None:
                    response = await self._client.post(SITEVERIFY_URL, data=form, timeout=TIMEOUT)
                else:
                    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                        response = await client.post(SITEVERIFY_URL, data=form)
            except httpx.HTTPError as exc:
                last = exc
                continue
            if response.status_code >= 500:
                last = RuntimeError(f"siteverify {response.status_code}")
                continue
            try:
                body: Any = response.json()
            except ValueError:
                break
            if isinstance(body, dict):
                return body  # type: ignore[return-value]  # reason: JSON object
            break
        log.warning("turnstile.unavailable", error=type(last).__name__ if last else "bad_response")
        raise UpstreamUnavailable("The bot check is unavailable right now.")
