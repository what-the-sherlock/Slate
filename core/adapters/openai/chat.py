"""ChatModel over the OpenAI API (ADR-0005): gpt-4o-mini by default (OPENAI_CHAT_MODEL).

The only module that imports the OpenAI SDK for chat (rule 6). Streams text deltas and
then exactly one Usage (``stream_options.include_usage``). The SDK retries the request
itself (before any token arrives) with backoff and Retry-After; a failure mid-stream isn't
retried, because the user has already seen part of the answer. Vendor errors become domain
errors. Never logs prompts or output (they contain document text).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

import openai
from openai import AsyncOpenAI

from core.domain.chat import ChatDelta, ChatMessage, Usage
from core.domain.errors import RateLimited, UpstreamUnavailable

DEFAULT_MODEL = "gpt-4o-mini"
TIMEOUT = openai.Timeout(60.0, connect=10.0)  # per read: a long answer can stream for a while
MAX_RETRIES = 2


class OpenAIChatModel:
    def __init__(
        self, api_key: str, *, model: str = DEFAULT_MODEL, client: AsyncOpenAI | None = None
    ) -> None:
        self._model = model
        self._client = client or AsyncOpenAI(
            api_key=api_key, timeout=TIMEOUT, max_retries=MAX_RETRIES
        )

    @property
    def model(self) -> str:
        return self._model

    async def stream(
        self, messages: Sequence[ChatMessage], *, max_tokens: int
    ) -> AsyncIterator[ChatDelta | Usage]:
        payload: list[Any] = [{"role": m.role.value, "content": m.content} for m in messages]
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=payload,
                max_tokens=max_tokens,
                temperature=0.1,  # grounded answers: little creativity wanted
                stream=True,
                stream_options={"include_usage": True},
            )
            tokens_in = tokens_out = 0
            async for chunk in response:
                for choice in chunk.choices:
                    if choice.delta.content:
                        yield ChatDelta(choice.delta.content)
                if chunk.usage is not None:
                    tokens_in = chunk.usage.prompt_tokens
                    tokens_out = chunk.usage.completion_tokens
            yield Usage(model=self._model, tokens_in=tokens_in, tokens_out=tokens_out)
        except openai.RateLimitError as e:
            raise RateLimited(
                "The AI service is busy. Try again shortly.", retry_after_s=_retry_after(e)
            ) from e
        except openai.AuthenticationError as e:
            err = UpstreamUnavailable("The AI service rejected the server's API key.")
            err.retryable = False
            raise err from e
        except (openai.BadRequestError, openai.UnprocessableEntityError) as e:
            err = UpstreamUnavailable(f"The AI service rejected the request ({e.status_code}).")
            err.retryable = False
            raise err from e
        except (openai.APITimeoutError, openai.APIConnectionError, openai.InternalServerError) as e:
            raise UpstreamUnavailable("The AI service is unavailable right now.") from e
        except openai.APIStatusError as e:
            raise UpstreamUnavailable(f"The AI service failed ({e.status_code}).") from e


def _retry_after(e: openai.RateLimitError) -> int | None:
    value = e.response.headers.get("retry-after")
    try:
        return int(float(value)) if value else None
    except ValueError:
        return None
