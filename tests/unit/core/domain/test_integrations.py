from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from hypothesis import given
from hypothesis import strategies as st

from core.domain.errors import NotFound, ValidationFailed
from core.domain.integrations import (
    GOOGLE_UNVERIFIED_NOTICE,
    REGISTRY,
    AuthKind,
    ConnectionStatus,
    IdentitySpec,
    IntegrationState,
    RefreshFailure,
    TokenSet,
    account_from,
    authorize_url,
    classify_failure,
    code_challenge,
    connection_state,
    derive_verifier,
    needs_refresh,
    parse_tokens,
    provider,
    secret_json,
    short_error,
    token_error,
    tokens_from_secret,
)

NOW = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------- registry


def test_registry_ids_are_unique_and_safe_for_the_schema() -> None:
    ids = [p.id for p in REGISTRY]
    assert len(ids) == len(set(ids))
    for spec in REGISTRY:
        assert spec.id.isascii()
        assert spec.id[0].isalpha()
        assert spec.id.replace("_", "").isalnum()


def test_every_oauth_provider_has_https_endpoints_and_a_client_family() -> None:
    for spec in REGISTRY:
        if spec.auth is AuthKind.OAUTH2:
            assert spec.oauth is not None, spec.id
            assert spec.client, spec.id
            urls = [spec.oauth.authorize_url, spec.oauth.token_url, spec.oauth.revoke_url]
            urls.append(spec.oauth.identity.url)
            assert all(u is None or u.startswith("https://") for u in urls), spec.id
        else:
            assert spec.oauth is None, spec.id
            assert spec.connect_path, spec.id  # the older flows link to their own pages


def test_google_providers_carry_the_unverified_app_notice() -> None:
    google = [p for p in REGISTRY if p.client == "google"]
    assert {p.id for p in google} == {"gmail", "google_calendar", "google_drive"}
    assert all(p.notice == GOOGLE_UNVERIFIED_NOTICE for p in google)
    assert "portfolio" in GOOGLE_UNVERIFIED_NOTICE
    assert "read-only" in GOOGLE_UNVERIFIED_NOTICE


def test_drive_uses_the_non_restricted_per_file_scope() -> None:
    drive = provider("google_drive").oauth
    assert drive is not None
    assert "https://www.googleapis.com/auth/drive.file" in drive.scopes
    assert not any(s.endswith("/drive") or s.endswith("drive.readonly") for s in drive.scopes)


def test_unknown_providers_are_not_found() -> None:
    with pytest.raises(NotFound):
        provider("../etc")


# ---------------------------------------------------------------- PKCE and URLs


def test_code_challenge_matches_the_rfc_7636_example() -> None:
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert code_challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


@given(st.text(min_size=1, max_size=64))
def test_derived_verifiers_are_valid_and_key_bound(nonce: str) -> None:
    v = derive_verifier(b"k" * 32, nonce)
    assert len(v) == 43
    assert all(c.isalnum() or c in "-_" for c in v)
    assert v == derive_verifier(b"k" * 32, nonce)
    assert v != derive_verifier(b"j" * 32, nonce)


def test_authorize_url_carries_state_scopes_pkce_and_extras() -> None:
    linear = provider("linear").oauth
    assert linear is not None
    url = authorize_url(
        linear, client_id="cid", redirect_uri="https://app.test/integrations/callback",
        state="st", challenge="ch",
    )  # fmt: skip
    parsed = urlparse(url)
    q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == "https://linear.app/oauth/authorize"
    assert q == {
        "client_id": "cid",
        "redirect_uri": "https://app.test/integrations/callback",
        "response_type": "code",
        "state": "st",
        "scope": "read",
        "code_challenge": "ch",
        "code_challenge_method": "S256",
        "actor": "user",
    }


def test_slack_asks_for_user_scopes_and_notion_skips_pkce() -> None:
    slack, notion = provider("slack").oauth, provider("notion").oauth
    assert slack is not None
    assert notion is not None
    q = parse_qs(urlparse(authorize_url(
        slack, client_id="c", redirect_uri="r", state="s", challenge="x",
    )).query)  # fmt: skip
    assert q["user_scope"] == ["channels:history,channels:read,users:read"]
    assert "code_challenge" not in q  # Slack's spec has pkce off
    nq = parse_qs(urlparse(authorize_url(
        notion, client_id="c", redirect_uri="r", state="s", challenge="x",
    )).query)  # fmt: skip
    assert nq["owner"] == ["user"]
    assert "scope" not in nq


# ---------------------------------------------------------------- token responses


def test_parse_tokens_reads_expiry_and_scopes() -> None:
    body = {"access_token": "a", "refresh_token": "r", "expires_in": 3600, "scope": "x y"}
    assert parse_tokens(body, NOW) == TokenSet("a", "r", NOW + timedelta(hours=1), ("x", "y"))


def test_parse_tokens_unwraps_slack_user_tokens_and_keeps_old_refresh() -> None:
    slack = {"ok": True, "authed_user": {"access_token": "xoxp", "scope": "a,b"}}
    assert parse_tokens(slack, NOW) == TokenSet("xoxp", None, None, ("a", "b"))
    refreshed = parse_tokens({"access_token": "new"}, NOW, keep_refresh="old")
    assert refreshed.refresh_token == "old"


@pytest.mark.parametrize("body", [{}, {"access_token": ""}, None, [], {"access_token": 5}])
def test_parse_tokens_refuses_a_response_without_a_token(body: object) -> None:
    with pytest.raises(ValidationFailed):
        parse_tokens(body, NOW)


def test_token_error_handles_http_errors_and_slacks_ok_false() -> None:
    assert token_error(200, {"access_token": "a"}) is None
    assert token_error(400, {"error": "invalid_grant"}) == "invalid_grant"
    assert token_error(502, "gateway") == "http_502"
    assert token_error(200, {"ok": False, "error": "invalid_code"}) == "invalid_code"


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (400, {"error": "invalid_grant"}, RefreshFailure.TERMINAL),
        (401, {"error": "invalid_client"}, RefreshFailure.TERMINAL),
        (200, {"ok": False, "error": "token_revoked"}, RefreshFailure.TERMINAL),
        (400, None, RefreshFailure.TERMINAL),
        (None, None, RefreshFailure.TRANSIENT),
        (429, {"error": "rate_limited"}, RefreshFailure.TRANSIENT),
        (503, {"error": "temporarily_unavailable"}, RefreshFailure.TRANSIENT),
        (400, {"error": "slow_down"}, RefreshFailure.TRANSIENT),
    ],
)
def test_refresh_failures_are_classified(
    status: int | None, body: object, expected: RefreshFailure
) -> None:
    assert classify_failure(status, body) is expected


def test_account_comes_from_dotted_paths_and_is_capped() -> None:
    linear = IdentitySpec("token", "organization.id", "organization.name")
    body = {"organization": {"id": 42, "name": "  Kestrel \n Freight " + "x" * 300}}
    account = account_from(linear, body)
    assert account.id == "42"
    assert account.label is not None
    assert account.label.startswith("Kestrel Freight")
    assert len(account.label) == 200
    with pytest.raises(ValidationFailed):
        account_from(linear, {"organization": {"name": "no id"}})


def test_secrets_round_trip_and_bad_ones_read_as_none() -> None:
    tokens = TokenSet("a", "r", NOW, ("x",))
    assert tokens_from_secret(secret_json(tokens)) == tokens
    assert tokens_from_secret("not json") is None
    assert tokens_from_secret('{"a": ""}') is None


def test_refresh_is_due_inside_the_skew_and_never_without_expiry() -> None:
    assert needs_refresh(NOW + timedelta(minutes=4), NOW)
    assert not needs_refresh(NOW + timedelta(minutes=6), NOW)
    assert not needs_refresh(None, NOW)


def test_stored_errors_keep_only_safe_characters() -> None:
    assert short_error("invalid_grant") == "invalid_grant"
    assert short_error("<script>bad</script>") == "scriptbadscript"
    assert len(short_error("x" * 999)) == 300


def test_any_connection_needing_reconnect_decides_the_state() -> None:
    assert connection_state([]) is IntegrationState.NOT_CONNECTED
    assert connection_state([ConnectionStatus.CONNECTED]) is IntegrationState.CONNECTED
    both = [ConnectionStatus.CONNECTED, ConnectionStatus.NEEDS_RECONNECT]
    assert connection_state(both) is IntegrationState.NEEDS_RECONNECT
