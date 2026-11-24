"""OpenAI adapter behaviour with a mock HTTP transport: no network, no real key, no cost."""

from __future__ import annotations

import json

import httpx2 as httpx  # the OpenAI SDK (v3) is built on httpx2
import pytest
from openai import AsyncOpenAI, DefaultAsyncHttpxClient

from core.adapters.openai.embedder import OpenAIEmbedder
from core.domain.errors import RateLimited, UpstreamUnavailable


def client(handler: httpx.MockTransport) -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key="sk-test-not-real",
        base_url="https://api.test/v1",
        max_retries=0,
        http_client=DefaultAsyncHttpxClient(transport=handler),
    )


def embedder(handler: httpx.MockTransport) -> OpenAIEmbedder:
    return OpenAIEmbedder("unused", dims=3, client=client(handler))


async def test_requests_1024_style_dimensions_and_restores_input_order() -> None:
    seen: dict[str, object] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        data = [
            {"object": "embedding", "index": 1, "embedding": [0.0, 1.0, 0.0]},
            {"object": "embedding", "index": 0, "embedding": [1.0, 0.0, 0.0]},
        ]
        return httpx.Response(
            200,
            json={"object": "list", "data": data, "model": "m",
                  "usage": {"prompt_tokens": 7, "total_tokens": 7}},
        )  # fmt: skip

    result = await embedder(httpx.MockTransport(handle)).embed_documents(["a", "b"])
    assert seen["dimensions"] == 3
    assert seen["model"] == "text-embedding-3-small"
    assert result.vectors == [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]  # sorted by index
    assert result.tokens == 7


async def test_rate_limit_maps_to_domain_error_with_retry_after() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429, headers={"retry-after": "12"}, json={"error": {"message": "slow"}}
        )

    with pytest.raises(RateLimited) as info:
        await embedder(httpx.MockTransport(handle)).embed_documents(["a"])
    assert info.value.retry_after_s == 12


async def test_bad_key_is_not_retryable() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    with pytest.raises(UpstreamUnavailable) as info:
        await embedder(httpx.MockTransport(handle)).embed_documents(["a"])
    assert info.value.retryable is False


async def test_server_errors_are_retryable_upstream_errors() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": {"message": "down"}})

    with pytest.raises(UpstreamUnavailable) as info:
        await embedder(httpx.MockTransport(handle)).embed_documents(["a"])
    assert info.value.retryable is True


async def test_empty_input_makes_no_request() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    result = await embedder(httpx.MockTransport(handle)).embed_documents([])
    assert result.vectors == []
