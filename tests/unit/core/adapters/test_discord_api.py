"""DiscordBot against httpx.MockTransport: no network."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from core.adapters.discord.api import API, DiscordBot, _route
from core.domain.errors import NotFound, PermissionDenied, RateLimited, UpstreamUnavailable

G = "100000000000000000"
CH = "100000000000000010"
USER = "100000000000000002"

Handler = Callable[[httpx.Request], httpx.Response]


def bot(handler: Handler, sleeps: list[float] | None = None) -> DiscordBot:
    async def sleep(s: float) -> None:
        if sleeps is not None:
            sleeps.append(s)

    return DiscordBot(
        bot_token="bot-token",
        client_id="123",
        client_secret="shh",
        redirect_uri="https://app.test/sources/discord/callback",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        sleep=sleep,
    )


B = 10**17  # snowflakes are 15-21 digits; tests use small offsets from this base


def message(mid: int, text: str = "hi") -> dict[str, object]:
    return {"id": str(B + mid), "channel_id": CH, "content": text, "type": 0,
            "author": {"id": USER, "username": "ana", "global_name": "Ana"}}  # fmt: skip


def test_install_url_asks_for_read_only_permissions() -> None:
    url = bot(lambda r: httpx.Response(200)).install_url("st")
    q = parse_qs(urlsplit(url).query)
    assert urlsplit(url).netloc == "discord.com"
    assert q["permissions"] == [str((1 << 10) | (1 << 16))]
    assert set(q["scope"][0].split()) == {"bot", "identify", "guilds.members.read"}
    assert q["state"] == ["st"]
    assert q["response_type"] == ["code"]


def test_route_keys_keep_major_ids_only() -> None:
    assert _route("GET", f"{API}/channels/{CH}/messages?after=1") == f"GET /channels/{CH}/messages"
    assert _route("GET", f"{API}/guilds/{G}/members/{USER}") == f"GET /guilds/{G}/members/{{id}}"


async def test_messages_after_pages_forward_in_order() -> None:
    pages = {0: [message(i) for i in range(200, 100, -1)], 200: [message(201), message(202)]}

    def handler(r: httpx.Request) -> httpx.Response:
        assert r.headers["authorization"] == "Bot bot-token"
        after = int(r.url.params["after"]) - B
        return httpx.Response(200, json=pages.get(max(after, 0), []))

    got = [m.id async for m in bot(handler).messages_after(CH, B, 1000)]
    assert got == [str(B + i) for i in range(101, 203)]


async def test_messages_after_respects_the_limit() -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        start = int(r.url.params["after"]) - B
        return httpx.Response(200, json=[message(start + i) for i in range(100, 0, -1)])

    got = [m async for m in bot(handler).messages_after(CH, B + 1000, 150)]
    assert len(got) == 150


async def test_latest_messages_walks_back_to_since() -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        before = int(r.url.params.get("before", str(B + 1000))) - B
        return httpx.Response(200, json=[message(i) for i in range(before - 1, before - 101, -1)])

    got = await bot(handler).latest_messages(CH, since=B + 850, limit=5000)
    assert [m.id for m in got] == [str(B + i) for i in range(851, 1000)]
    capped = await bot(handler).latest_messages(CH, since=B, limit=30)
    assert [m.id for m in capped] == [str(B + i) for i in range(970, 1000)]  # the newest 30


async def test_429_waits_and_retries() -> None:
    calls = {"n": 0}
    sleeps: list[float] = []

    def handler(r: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"retry_after": 1.5, "global": False})
        return httpx.Response(200, json={"id": G, "name": "Acme", "owner_id": USER, "roles": []})

    g = await bot(handler, sleeps).guild(G)
    assert g.name == "Acme"
    assert sleeps
    assert 1.0 <= sleeps[0] <= 1.5


async def test_long_429_becomes_rate_limited() -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"retry_after": 600, "global": True})

    with pytest.raises(RateLimited) as e:
        await bot(handler).guild(G)
    assert (e.value.retry_after_s or 0) >= 600


async def test_exhausted_bucket_is_waited_out_before_the_next_call() -> None:
    sleeps: list[float] = []

    def handler(r: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"id": G, "name": "A", "owner_id": USER, "roles": []},
            headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset-after": "2"},
        )

    b = bot(handler, sleeps)
    await b.guild(G)
    await b.guild(G)
    assert sleeps
    assert sleeps[0] > 1.5


@pytest.mark.parametrize(
    ("status", "code", "error"),
    [(403, 50001, PermissionDenied), (404, 10003, NotFound), (401, 0, UpstreamUnavailable),
     (500, 0, UpstreamUnavailable)],
)  # fmt: skip
async def test_errors_are_typed(status: int, code: int, error: type[Exception]) -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"code": code, "message": "x"})

    with pytest.raises(error):
        await bot(handler).channels(G)


async def test_transport_errors_retry_then_fail() -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(UpstreamUnavailable):
        await bot(handler).guild(G)


async def test_ids_are_validated_before_any_request() -> None:
    def handler(r: httpx.Request) -> httpx.Response:  # pragma: no cover - must not be called
        raise AssertionError("no request expected")

    with pytest.raises(ValueError, match="invalid Discord id"):
        await bot(handler).guild("../users/@me")


def _install_handler(guild: str, scope: str, log: list[str]) -> Handler:
    def handler(r: httpx.Request) -> httpx.Response:
        log.append(r.url.path)
        if r.url.path.endswith("/oauth2/token"):
            body = parse_qs(r.content.decode())
            assert body["grant_type"] == ["authorization_code"]
            assert body["redirect_uri"] == ["https://app.test/sources/discord/callback"]
            return httpx.Response(
                200, json={"access_token": "user-token", "scope": scope, "guild": {"id": guild}}
            )
        if r.url.path.endswith("/member"):
            assert r.headers["authorization"] == "Bearer user-token"
            return httpx.Response(200, json={"user": {"id": USER}, "roles": ["5" * 18]})
        if r.url.path.endswith("/revoke"):
            return httpx.Response(200, json={})
        raise AssertionError(r.url.path)

    return handler


async def test_verify_install_proves_the_guild_and_reads_roles() -> None:
    log: list[str] = []
    proof = await bot(_install_handler(G, "bot identify guilds.members.read", log)).verify_install(
        "abcdefghij12", G
    )
    assert proof is not None
    assert proof.member.user_id == USER
    assert proof.member.roles == ("5" * 18,)
    assert log[-1].endswith("/oauth2/token/revoke")  # always revoked


@pytest.mark.parametrize(
    ("guild", "scope"),
    [("999999999999999999", "bot identify guilds.members.read"), (G, "bot identify")],
    ids=["other-guild", "missing-scope"],
)
async def test_verify_install_refuses_and_still_revokes(guild: str, scope: str) -> None:
    log: list[str] = []
    assert await bot(_install_handler(guild, scope, log)).verify_install("abcdefghij12", G) is None
    assert log[-1].endswith("/revoke")
    assert not any(p.endswith("/member") for p in log)


async def test_verify_install_rejects_malformed_input_without_calling_discord() -> None:
    def handler(r: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no request expected")

    b = bot(handler)
    assert await b.verify_install("short", G) is None
    assert await b.verify_install("abcdefghij12", "not-a-guild") is None
    assert await b.verify_install("abc def ghi jkl", G) is None


async def test_rejected_code_returns_none() -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    assert await bot(handler).verify_install("abcdefghij12", G) is None


async def test_archived_threads_stop_at_the_floor() -> None:
    def ts(day: int) -> str:
        return datetime(2026, 11, day, tzinfo=UTC).isoformat()

    page = {
        "threads": [
            {"id": "100000000000000101", "parent_id": CH, "name": "new", "type": 11,
             "thread_metadata": {"archived": True, "archive_timestamp": ts(20)}},
            {"id": "100000000000000102", "parent_id": CH, "name": "old", "type": 11,
             "thread_metadata": {"archived": True, "archive_timestamp": ts(2)}},
        ],
        "has_more": True,
    }  # fmt: skip

    def handler(r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(page).encode())

    got = [
        t.name async for t in bot(handler).archived_threads(CH, datetime(2026, 11, 10, tzinfo=UTC))
    ]
    assert got == ["new"]


async def test_leave_guild_never_raises() -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    await bot(handler).leave_guild(G)  # logged, not raised
    await bot(handler).leave_guild("bad")  # ignored


def test_configured_needs_everything() -> None:
    assert bot(lambda r: httpx.Response(200)).configured
    assert not DiscordBot(
        bot_token="t", client_id=None, client_secret="s", redirect_uri="x"
    ).configured
