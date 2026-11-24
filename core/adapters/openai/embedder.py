"""Embedder over the OpenAI API (ADR-0004): text-embedding-3-small at 1024 dimensions.

The only module that imports the OpenAI SDK for embeddings (rule 6). Explicit timeouts;
the SDK retries idempotent failures with exponential backoff and honours Retry-After.
Vendor errors become domain errors here. Never logs inputs (they are document text).
"""

from __future__ import annotations

from collections.abc import Sequence

import openai
from openai import AsyncOpenAI

from core.domain.documents import Vector
from core.domain.errors import RateLimited, UpstreamUnavailable
from core.ports.models import Embeddings

TIMEOUT_S = 30.0
MAX_RETRIES = 4
MAX_BATCH = 96


class OpenAIEmbedder:
    def __init__(
        self,
        api_key: str,
        *,
        model: str = "text-embedding-3-small",
        dims: int = 1024,
        client: AsyncOpenAI | None = None,
    ) -> None:
        self._model = model
        self._dims = dims
        self._client = client or AsyncOpenAI(
            api_key=api_key, timeout=TIMEOUT_S, max_retries=MAX_RETRIES
        )

    @property
    def model(self) -> str:
        return self._model

    @property
    def dims(self) -> int:
        return self._dims

    async def embed_documents(self, texts: Sequence[str]) -> Embeddings:
        if not texts:
            return Embeddings(vectors=[], tokens=0)
        if len(texts) > MAX_BATCH:
            raise ValueError(f"batch too large ({len(texts)} > {MAX_BATCH})")
        try:
            response = await self._client.embeddings.create(
                model=self._model, input=list(texts), dimensions=self._dims
            )
        except openai.RateLimitError as e:
            retry_after = _retry_after(e)
            raise RateLimited("OpenAI rate limit reached.", retry_after_s=retry_after) from e
        except openai.AuthenticationError as e:
            err = UpstreamUnavailable("OpenAI rejected the API key.")
            err.retryable = False  # retrying won't fix a bad key
            raise err from e
        except (openai.APITimeoutError, openai.APIConnectionError, openai.InternalServerError) as e:
            raise UpstreamUnavailable(f"OpenAI is unavailable ({type(e).__name__}).") from e
        except (openai.BadRequestError, openai.UnprocessableEntityError) as e:
            err = UpstreamUnavailable(f"OpenAI rejected the input ({e.status_code}).")
            err.retryable = False  # the same input will be rejected again
            raise err from e
        except openai.APIStatusError as e:
            raise UpstreamUnavailable(f"OpenAI request failed ({e.status_code}).") from e
        ordered = sorted(response.data, key=lambda d: d.index)
        if len(ordered) != len(texts):
            raise UpstreamUnavailable("OpenAI returned the wrong number of embeddings.")
        return Embeddings(
            vectors=[list(d.embedding) for d in ordered], tokens=response.usage.prompt_tokens
        )

    async def embed_query(self, text: str) -> Vector:
        result = await self.embed_documents([text])
        return result.vectors[0]


def _retry_after(e: openai.RateLimitError) -> int | None:
    value = e.response.headers.get("retry-after")
    try:
        return int(float(value)) if value else None
    except ValueError:
        return None
