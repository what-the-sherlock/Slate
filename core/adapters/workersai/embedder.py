"""BGE-M3 on Cloudflare Workers AI (ADR-0004): the second embedder, used for evals.

Free tier (10k neurons/day), so ADR-0012 applies: Cloudflare may retain inputs, and only
PUBLIC chunks may ever be sent here. The generation this embedder builds is created with
``data_scope='public'`` and a database trigger rejects private chunks under it; the
backfill only selects public chunks. Callers must still never pass private text.

Plain httpx (no vendor SDK). Explicit timeouts; idempotent retries with exponential
backoff + jitter that honour ``Retry-After``; failures become typed domain errors. The
API token travels only in the Authorization header and is never logged.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any, cast

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_random_exponential,
)
from tenacity.wait import wait_base

from core.domain.documents import Vector
from core.domain.errors import AppError, RateLimited, UpstreamUnavailable
from core.ports.models import Embeddings

MODEL = "@cf/baai/bge-m3"
DIMS = 1024
MAX_BATCH = 50
TIMEOUT = httpx.Timeout(30.0, connect=5.0)
MAX_ATTEMPTS = 4
BASE_URL = "https://api.cloudflare.com/client/v4"
_ACCOUNT_ID = re.compile(r"^[0-9a-f]{32}$")


def _retryable(exc: BaseException) -> bool:
    return isinstance(exc, AppError) and exc.retryable


class _WaitRetryAfter(wait_base):
    """Use the server's Retry-After when given (capped), else exponential + jitter."""

    def __init__(self) -> None:
        self._fallback = wait_random_exponential(multiplier=0.5, max=20)

    def __call__(self, retry_state: Any) -> float:  # reason: tenacity's RetryCallState
        outcome = retry_state.outcome
        exc = outcome.exception() if outcome is not None else None
        if isinstance(exc, RateLimited):
            after = exc.extra.get("retry_after_s")
            if isinstance(after, int) and after >= 0:
                return float(min(after, 60))
        return float(self._fallback(retry_state))


class WorkersAIEmbedder:
    def __init__(
        self,
        account_id: str,
        api_token: str,
        *,
        model: str = MODEL,
        client: httpx.AsyncClient | None = None,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        if not _ACCOUNT_ID.match(account_id):
            raise ValueError("CF_ACCOUNT_ID must be the 32-character hex account id")
        if not api_token.strip():
            raise ValueError("CF_API_TOKEN is empty")
        self._url = f"{BASE_URL}/accounts/{account_id}/ai/run/{model}"
        self._model = model
        self._headers = {"Authorization": f"Bearer {api_token.strip()}"}
        self._client = client or httpx.AsyncClient(timeout=TIMEOUT)
        self._attempts = max_attempts

    @property
    def model(self) -> str:
        return self._model

    @property
    def dims(self) -> int:
        return DIMS

    async def embed_documents(self, texts: Sequence[str]) -> Embeddings:
        if not texts:
            return Embeddings(vectors=[], tokens=0)
        if len(texts) > MAX_BATCH:
            raise ValueError(f"batch too large ({len(texts)} > {MAX_BATCH})")
        vectors: list[Vector] = []
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self._attempts),
            wait=_WaitRetryAfter(),
            retry=retry_if_exception(_retryable),
            reraise=True,
        ):
            with attempt:
                vectors = await self._call(list(texts))
        # Workers AI reports no token usage; ~4 characters per token is close enough for
        # the ledger (cost is $0 on the free tier either way).
        return Embeddings(vectors=vectors, tokens=sum(len(t) for t in texts) // 4)

    async def embed_query(self, text: str) -> Vector:
        return (await self.embed_documents([text])).vectors[0]

    async def _call(self, texts: list[str]) -> list[Vector]:
        try:
            r = await self._client.post(self._url, json={"text": texts}, headers=self._headers)
        except httpx.TimeoutException as e:
            raise UpstreamUnavailable("Workers AI timed out.") from e
        except httpx.TransportError as e:
            raise UpstreamUnavailable(f"Workers AI is unreachable ({type(e).__name__}).") from e
        if r.status_code == 429:
            raise RateLimited("Workers AI rate limit reached.", retry_after_s=_retry_after(r))
        if r.status_code in (401, 403):
            err = UpstreamUnavailable("Workers AI rejected the API token.")
            err.retryable = False  # retrying won't fix a bad token
            raise err
        if 400 <= r.status_code < 500:
            err = UpstreamUnavailable(f"Workers AI rejected the input ({r.status_code}).")
            err.retryable = False
            raise err
        if r.status_code >= 500:
            raise UpstreamUnavailable(f"Workers AI failed ({r.status_code}).")
        return _parse(r, len(texts))


def _parse(r: httpx.Response, expected: int) -> list[Vector]:
    try:
        body: Any = r.json()  # reason: untrusted JSON, validated below
        data: Any = body["result"]["data"]
        ok = body.get("success", True) is True and isinstance(data, list)
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        raise UpstreamUnavailable("Workers AI returned an unreadable response.") from e
    rows = cast(list[Any], data) if ok else []
    if not ok or len(rows) != expected:
        raise UpstreamUnavailable("Workers AI returned the wrong number of embeddings.")
    vectors: list[Vector] = []
    for row in rows:
        values = cast(list[Any], row) if isinstance(row, list) else []
        if len(values) != DIMS:
            raise UpstreamUnavailable("Workers AI returned a vector of the wrong size.")
        try:
            vectors.append([float(x) for x in values])
        except (TypeError, ValueError) as e:
            raise UpstreamUnavailable("Workers AI returned a non-numeric vector.") from e
    return vectors


def _retry_after(r: httpx.Response) -> int | None:
    value = r.headers.get("retry-after")
    try:
        return int(float(value)) if value else None
    except ValueError:
        return None
