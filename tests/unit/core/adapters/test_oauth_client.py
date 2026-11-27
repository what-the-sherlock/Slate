from __future__ import annotations

import base64
import json
from urllib.parse import parse_qs

import httpx
import pytest

from core.adapters.oauth.client import HttpOAuthProvider
from core.domain.errors import ReconnectRequired, UpstreamUnavailable, ValidationFailed
from core.domain.integrations import OAuthSpec, provider
from core.ports.integrations import ClientCredentials

CLIENT = ClientCredentials("cid", "csecret")
REDIRECT = "https://slate.test/integrations/callback"


def client_for(
    responses: dict[str, httpx.Response],
) -> tuple[HttpOAuthProvider, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return responses[f"{request.method} {request.url.copy_with(query=None)}"]

    http = httpx.AsyncClient(transport=httpx.MockTransport(handle), follow_redirects=False)
    return HttpOAuthProvider(http), seen


def oauth(provider_id: str) -> OAuthSpec:
    spec = provider(provider_id).oauth
    assert spec is not None
    return spec


async def test_linear_exchange_posts_a_form_with_the_verifier_and_reads_the_account() -> None:
    body = {"access_token": "a", "expires_in": 3600, "organization": {"id": "o1", "name": "K"}}
    p, seen = client_for(
        {"POST https://api.linear.app/oauth/token": httpx.Response(200, json=body)}
    )
    tokens, account = await p.exchange(oauth("linear"), CLIENT, "code-1", "ver-1", REDIRECT)
    assert tokens.access_token == "a"
    assert (account.id, account.label) == ("o1", "K")
    form = parse_qs(seen[0].content.decode())
    assert form["grant_type"] == ["authorization_code"]
    assert form["code_verifier"] == ["ver-1"]
    assert form["client_secret"] == ["csecret"]
    assert form["redirect_uri"] == [REDIRECT]


async def test_notion_exchange_uses_basic_auth_and_a_json_body() -> None:
    body = {"access_token": "n", "workspace_id": "w1", "workspace_name": "Kestrel"}
    p, seen = client_for(
        {"POST https://api.notion.com/v1/oauth/token": httpx.Response(200, json=body)}
    )
    _, account = await p.exchange(oauth("notion"), CLIENT, "code-1", None, REDIRECT)
    assert account.label == "Kestrel"
    sent = json.loads(seen[0].content)
    assert "client_secret" not in sent  # in the Authorization header instead
    assert "code_verifier" not in sent
    expected = "Basic " + base64.b64encode(b"cid:csecret").decode()
    assert seen[0].headers["authorization"] == expected


async def test_google_reads_the_account_from_userinfo_with_the_new_token() -> None:
    p, seen = client_for({
        "POST https://oauth2.googleapis.com/token": httpx.Response(
            200, json={"access_token": "g", "refresh_token": "r", "expires_in": 3599}
        ),
        "GET https://openidconnect.googleapis.com/v1/userinfo": httpx.Response(
            200, json={"sub": "123", "email": "ada@example.test"}
        ),
    })  # fmt: skip
    tokens, account = await p.exchange(oauth("gmail"), CLIENT, "c", "v", REDIRECT)
    assert tokens.refresh_token == "r"
    assert (account.id, account.label) == ("123", "ada@example.test")
    assert seen[1].headers["authorization"] == "Bearer g"


async def test_a_refused_code_is_a_validation_error_without_the_body() -> None:
    p, _ = client_for({
        "POST https://api.linear.app/oauth/token": httpx.Response(
            400, json={"error": "invalid_grant", "error_description": "secret detail"}
        )
    })  # fmt: skip
    with pytest.raises(ValidationFailed) as err:
        await p.exchange(oauth("linear"), CLIENT, "c", "v", REDIRECT)
    assert "secret detail" not in str(err.value)


async def test_slack_ok_false_is_a_refusal_even_with_status_200() -> None:
    p, _ = client_for({
        "POST https://slack.com/api/oauth.v2.access": httpx.Response(
            200, json={"ok": False, "error": "invalid_code"}
        )
    })  # fmt: skip
    with pytest.raises(ValidationFailed):
        await p.exchange(oauth("slack"), CLIENT, "c", None, REDIRECT)


async def test_refresh_keeps_the_old_refresh_token_when_none_comes_back() -> None:
    p, seen = client_for({
        "POST https://oauth2.googleapis.com/token": httpx.Response(
            200, json={"access_token": "g2", "expires_in": 3599}
        )
    })  # fmt: skip
    tokens = await p.refresh(oauth("gmail"), CLIENT, "r-old")
    assert (tokens.access_token, tokens.refresh_token) == ("g2", "r-old")
    assert parse_qs(seen[0].content.decode())["grant_type"] == ["refresh_token"]


async def test_a_dead_grant_on_refresh_means_reconnect() -> None:
    p, _ = client_for({
        "POST https://oauth2.googleapis.com/token": httpx.Response(
            400, json={"error": "invalid_grant"}
        )
    })  # fmt: skip
    with pytest.raises(ReconnectRequired):
        await p.refresh(oauth("gmail"), CLIENT, "r")


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_provider_trouble_on_refresh_is_transient(status: int) -> None:
    p, seen = client_for({
        "POST https://oauth2.googleapis.com/token": httpx.Response(status, json={"error": "x"})
    })  # fmt: skip
    with pytest.raises(UpstreamUnavailable):
        await p.refresh(oauth("gmail"), CLIENT, "r")
    assert len(seen) == 1  # never retried: refresh tokens are single use


async def test_network_errors_are_transient() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow", request=request)

    p = HttpOAuthProvider(httpx.AsyncClient(transport=httpx.MockTransport(boom)))
    with pytest.raises(UpstreamUnavailable):
        await p.refresh(oauth("gmail"), CLIENT, "r")


async def test_a_redirect_from_the_token_endpoint_is_not_followed() -> None:
    p, seen = client_for({
        "POST https://api.linear.app/oauth/token": httpx.Response(
            302, headers={"location": "https://evil.test/steal"}
        )
    })  # fmt: skip
    with pytest.raises(ValidationFailed):
        await p.exchange(oauth("linear"), CLIENT, "c", "v", REDIRECT)
    assert [str(r.url.host) for r in seen] == ["api.linear.app"]


async def test_revoke_is_best_effort_and_skipped_without_an_endpoint() -> None:
    p, seen = client_for({
        "POST https://oauth2.googleapis.com/revoke": httpx.Response(503),
    })  # fmt: skip
    await p.revoke(oauth("gmail"), CLIENT, "tok")  # a failure doesn't raise
    await p.revoke(oauth("notion"), CLIENT, "tok")  # Notion has no revoke endpoint
    assert len(seen) == 1
    assert parse_qs(seen[0].content.decode())["token"] == ["tok"]
