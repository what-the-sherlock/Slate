"""Workers AI adapter with a mock HTTP transport: no network, no real token, no quota."""

from __future__ import annotations

import json

import httpx
import pytest

from core.adapters.workersai.embedder import DIMS, WorkersAIEmbedder
from core.domain.errors import RateLimited, UpstreamUnavailable

ACCOUNT = "0123456789abcdef0123456789abcdef"


def make(handler: httpx.MockTransport, *, attempts: int = 4) -> WorkersAIEmbedder:
    return WorkersAIEmbedder(
        ACCOUNT,
        "cf-test-not-real",
        client=httpx.AsyncClient(transport=handler),
        max_attempts=attempts,
    )


def ok(n: int) -> httpx.Response:
    data = [[float(i)] * DIMS for i in range(n)]
    return httpx.Response(200, json={"success": True, "result": {"shape": [n, DIMS], "data": data}})


async def test_posts_texts_with_bearer_token_and_returns_vectors_in_order() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return ok(len(json.loads(request.content)["text"]))

    result = await make(httpx.MockTransport(handle)).embed_documents(["a", "bb"])
    assert [v[0] for v in result.vectors] == [0.0, 1.0]
    assert len(result.vectors[0]) == DIMS
    req = seen[0]
    assert req.url.path == f"/client/v4/accounts/{ACCOUNT}/ai/run/@cf/baai/bge-m3"
    assert req.headers["authorization"] == "Bearer cf-test-not-real"
    assert json.loads(req.content) == {"text": ["a", "bb"]}


async def test_retries_server_errors_then_succeeds() -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503) if calls < 3 else ok(1)

    result = await make(httpx.MockTransport(handle)).embed_documents(["x"])
    assert calls == 3
    assert len(result.vectors) == 1


async def test_rate_limit_honours_retry_after_and_gives_up_typed() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "0"})

    with pytest.raises(RateLimited):
        await make(httpx.MockTransport(handle), attempts=2).embed_documents(["x"])


@pytest.mark.parametrize("status", [401, 403, 400])
async def test_client_errors_are_final_and_not_retried(status: int) -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status)

    with pytest.raises(UpstreamUnavailable) as info:
        await make(httpx.MockTransport(handle)).embed_documents(["x"])
    assert info.value.retryable is False
    assert calls == 1


@pytest.mark.parametrize(
    "body",
    [
        {"success": True, "result": {"data": [[0.1, 0.2]]}},  # wrong size
        {"success": True, "result": {"data": []}},  # wrong count
        {"success": False, "result": {"data": [[0.0] * DIMS]}},
        {"unexpected": 1},
    ],
)
async def test_malformed_responses_become_upstream_errors(body: object) -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=body))
    with pytest.raises(UpstreamUnavailable):
        await make(transport, attempts=1).embed_documents(["x"])


async def test_timeouts_become_upstream_errors() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(UpstreamUnavailable, match="timed out"):
        await make(httpx.MockTransport(handle), attempts=1).embed_documents(["x"])


def test_rejects_bad_config_and_batches() -> None:
    with pytest.raises(ValueError, match="account id"):
        WorkersAIEmbedder("../../evil", "t")
    with pytest.raises(ValueError, match="empty"):
        WorkersAIEmbedder(ACCOUNT, "  ")


async def test_empty_and_oversized_batches() -> None:
    e = make(httpx.MockTransport(lambda r: ok(0)))
    assert (await e.embed_documents([])).vectors == []
    with pytest.raises(ValueError, match="batch too large"):
        await e.embed_documents(["x"] * 51)
