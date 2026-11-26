from __future__ import annotations

from fastapi.testclient import TestClient

from tests.fakes.auth import FakeMailer
from tests.fakes.mailbox import FakeMailbox
from tests.unit.api.accounts import csrf, sign_up

PASSWORD = "abcdefghijklmnop"


def test_status_lists_providers(client: TestClient, mailer: FakeMailer) -> None:
    sign_up(client, mailer)
    body = client.get("/api/v1/sources/email/status").json()
    assert body["available"] is True
    assert {p["id"] for p in body["providers"]} == {"gmail", "yahoo", "icloud", "fastmail"}


def test_connect_sync_and_disconnect(
    client: TestClient, mailer: FakeMailer, fake_mailbox: FakeMailbox
) -> None:
    sign_up(client, mailer)
    r = client.post(
        "/api/v1/sources/email/connect",
        json={
            "provider": "gmail",
            "address": "me@gmail.com",
            "app_password": "abcd efgh ijkl mnop",
        },
        headers=csrf(client),
    )
    assert r.status_code == 201, r.text
    assert PASSWORD not in r.text
    sid = r.json()["source_id"]
    assert client.post(f"/api/v1/sources/{sid}/sync", headers=csrf(client)).status_code == 200
    assert client.delete(f"/api/v1/sources/{sid}", headers=csrf(client)).status_code == 204


def test_wrong_password_is_403_and_never_echoed(client: TestClient, mailer: FakeMailer) -> None:
    sign_up(client, mailer)
    r = client.post(
        "/api/v1/sources/email/connect",
        json={"provider": "gmail", "address": "me@gmail.com", "app_password": "secret-guess-123"},
        headers=csrf(client),
    )
    assert r.status_code == 403
    assert "secret-guess-123" not in r.text


def test_unknown_provider_and_missing_csrf(client: TestClient, mailer: FakeMailer) -> None:
    sign_up(client, mailer)
    body = {"provider": "evil.example.com", "address": "me@gmail.com", "app_password": PASSWORD}
    r = client.post("/api/v1/sources/email/connect", json=body, headers=csrf(client))
    assert r.status_code == 422
    assert PASSWORD not in r.text
    assert client.post("/api/v1/sources/email/connect", json=body).status_code == 403
