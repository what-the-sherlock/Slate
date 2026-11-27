from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from tests.fakes.auth import FakeMailer
from tests.fakes.integrations import FakeOAuth
from tests.unit.api.accounts import sign_up


def by_id(client: TestClient) -> dict[str, dict[str, object]]:
    r = client.get("/api/v1/integrations")
    assert r.status_code == 200, r.text
    return {i["id"]: i for i in r.json()}


def connect(client: TestClient, headers: dict[str, str]) -> dict[str, object]:
    r = client.post("/api/v1/integrations/linear/authorize", headers=headers)
    assert r.status_code == 200, r.text
    state = parse_qs(urlparse(r.json()["authorize_url"]).query)["state"][0]
    body = {"code": "the-code", "state": state}
    r = client.post("/api/v1/integrations/linear/callback", json=body, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def test_the_catalogue_lists_every_provider_with_its_state(
    client: TestClient, mailer: FakeMailer
) -> None:
    sign_up(client, mailer)
    got = by_id(client)
    assert got["linear"]["state"] == "not_connected"
    assert got["gmail"]["state"] == "not_set_up"  # no Google client in tests
    assert got["hubspot"]["state"] == "coming_soon"
    assert got["gmail"]["notice"]
    assert got["github"]["connect_path"] == "/sources?add=github"
    assert got["google_calendar"]["mode"] == "indexed"  # ADR-0033
    assert got["hubspot"]["mode"] == "live"


def test_connect_then_disconnect(
    client: TestClient, mailer: FakeMailer, fake_oauth: FakeOAuth
) -> None:
    headers = sign_up(client, mailer)
    conn = connect(client, headers)
    assert conn["status"] == "connected"
    assert conn["account_label"] == "ada@example.test"
    assert "token" not in str(conn).lower()  # no token ever leaves the API
    linear = by_id(client)["linear"]
    assert linear["state"] == "connected"
    assert [c["id"] for c in linear["connections"]] == [conn["id"]]  # type: ignore[index]

    r = client.delete(f"/api/v1/integrations/connections/{conn['id']}", headers=headers)
    assert r.status_code == 204
    assert fake_oauth.revoked == ["refresh-1"]
    assert by_id(client)["linear"]["state"] == "not_connected"


def test_a_state_from_another_account_is_refused(client: TestClient, mailer: FakeMailer) -> None:
    first = sign_up(client, mailer, email="first@example.test")
    r = client.post("/api/v1/integrations/linear/authorize", headers=first)
    state = parse_qs(urlparse(r.json()["authorize_url"]).query)["state"][0]
    client.cookies.clear()
    second = sign_up(client, mailer, email="second@example.test")
    body = {"code": "the-code", "state": state}
    r = client.post("/api/v1/integrations/linear/callback", json=body, headers=second)
    assert r.status_code == 422
    assert r.headers["content-type"].startswith("application/problem+json")


def test_changing_routes_need_csrf(client: TestClient, mailer: FakeMailer) -> None:
    sign_up(client, mailer)
    assert client.post("/api/v1/integrations/linear/authorize").status_code == 403
    body = {"code": "c", "state": "s"}
    assert client.post("/api/v1/integrations/linear/callback", json=body).status_code == 403


def test_unknown_and_unconnectable_providers(client: TestClient, mailer: FakeMailer) -> None:
    headers = sign_up(client, mailer)
    assert client.post("/api/v1/integrations/nope/authorize", headers=headers).status_code == 404
    assert client.post("/api/v1/integrations/gmail/authorize", headers=headers).status_code == 422


def test_someone_elses_connection_cannot_be_deleted(client: TestClient, mailer: FakeMailer) -> None:
    owner = sign_up(client, mailer, email="owner@example.test")
    conn = connect(client, owner)
    client.cookies.clear()
    other = sign_up(client, mailer, email="other@example.test")
    r = client.delete(f"/api/v1/integrations/connections/{conn['id']}", headers=other)
    assert r.status_code == 404


def test_reading_the_catalogue_needs_a_session(client: TestClient) -> None:
    assert client.get("/api/v1/integrations").status_code == 401
