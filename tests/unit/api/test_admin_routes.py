"""Admin console routes (ADR-0042): hidden from everyone but the verified admin."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.fakes.auth import FakeMailer
from tests.unit.api.accounts import PASSWORD, SAME_SITE, sign_up
from tests.unit.api.conftest import ADMIN_EMAIL

USER_KEYS = {
    "id", "email", "name", "joined_at", "email_verified", "disabled",
    "last_active_at", "workspaces", "you",
}  # fmt: skip
EVENT_KEYS = {"id", "kind", "at", "user_id", "user_email"}
READS = (
    "/api/v1/admin/overview",
    "/api/v1/admin/signups?days=30",
    "/api/v1/admin/failed-logins",
    "/api/v1/admin/users",
    "/api/v1/admin/events",
)
FORBIDDEN = ("password", "hash", "token", "csrf", "ip", "user_agent", "session")


def test_ordinary_users_get_404_everywhere(client: TestClient, mailer: FakeMailer) -> None:
    headers = sign_up(client, mailer, email="bob@acme.dev")
    for path in READS:
        assert client.get(path).status_code == 404, path
    r = client.patch(
        "/api/v1/admin/users/00000000-0000-0000-0000-000000000000",
        json={"disabled": True}, headers=headers,
    )  # fmt: skip
    assert r.status_code == 404
    assert client.get("/api/v1/me").json()["is_platform_admin"] is False


def test_signed_out_gets_401(client: TestClient) -> None:
    assert client.get("/api/v1/admin/overview").status_code == 401


def test_unverified_admin_address_is_not_admin(client: TestClient, mailer: FakeMailer) -> None:
    sign_up(client, mailer, email=ADMIN_EMAIL, verify=False)
    assert client.get("/api/v1/admin/overview").status_code == 404


def test_admin_reads_expose_only_allow_listed_fields(
    client: TestClient, mailer: FakeMailer
) -> None:
    sign_up(client, mailer, email="bob@acme.dev")
    sign_up(client, mailer, email=ADMIN_EMAIL)
    for path in READS:
        r = client.get(path)
        assert r.status_code == 200, (path, r.text)
        body = r.text.lower()
        for word in FORBIDDEN:
            assert f'"{word}' not in body, (path, word)
    users = client.get("/api/v1/admin/users").json()
    assert users["total"] == 2
    assert all(set(u) == USER_KEYS for u in users["items"])
    events = client.get("/api/v1/admin/events").json()["items"]
    assert events
    assert all(set(e) == EVENT_KEYS for e in events)
    assert client.get("/api/v1/me").json()["is_platform_admin"] is True


def test_disable_needs_csrf_then_blocks_sign_in(client: TestClient, mailer: FakeMailer) -> None:
    sign_up(client, mailer, email="bob@acme.dev")
    headers = sign_up(client, mailer, email=ADMIN_EMAIL)
    bob = next(
        u for u in client.get("/api/v1/admin/users").json()["items"] if u["email"] == "bob@acme.dev"
    )
    path = f"/api/v1/admin/users/{bob['id']}"
    assert client.patch(path, json={"disabled": True}).status_code == 403  # no CSRF header
    r = client.patch(path, json={"disabled": True}, headers=headers)
    assert r.status_code == 200
    assert r.json()["disabled"] is True
    r = client.post(
        "/api/v1/auth/login",
        json={"email": "bob@acme.dev", "password": PASSWORD},
        headers=SAME_SITE,
    )
    assert r.status_code == 401  # same generic answer as a wrong password


def test_admin_cannot_disable_self(client: TestClient, mailer: FakeMailer) -> None:
    headers = sign_up(client, mailer, email=ADMIN_EMAIL)
    me = client.get("/api/v1/me").json()["user"]["id"]
    r = client.patch(f"/api/v1/admin/users/{me}", json={"disabled": True}, headers=headers)
    assert r.status_code == 403


def test_query_validation(client: TestClient, mailer: FakeMailer) -> None:
    sign_up(client, mailer, email=ADMIN_EMAIL)
    assert client.get("/api/v1/admin/signups?days=7").status_code == 422
    assert client.get("/api/v1/admin/users?status=root").status_code == 422
    assert client.get("/api/v1/admin/users?limit=500").status_code == 422
    assert client.get("/api/v1/admin/users?cursor=abc").status_code == 422


def test_delete_account_then_sign_up_again(client: TestClient, mailer: FakeMailer) -> None:
    sign_up(client, mailer, email="bob@acme.dev")
    headers = sign_up(client, mailer, email=ADMIN_EMAIL)
    users = client.get("/api/v1/admin/users").json()["items"]
    bob = next(u for u in users if u["email"] == "bob@acme.dev")
    path = f"/api/v1/admin/users/{bob['id']}/delete"
    assert client.post(path, json={"confirm_email": "bob@acme.dev"}).status_code == 403  # CSRF
    assert (
        client.post(path, json={"confirm_email": "nope@acme.dev"}, headers=headers).status_code
        == 422
    )
    assert (
        client.post(path, json={"confirm_email": "bob@acme.dev"}, headers=headers).status_code
        == 204
    )
    kinds = [e["kind"] for e in client.get("/api/v1/admin/events").json()["items"]]
    assert kinds[0] == "account_deleted"
    client.cookies.clear()
    sign_up(client, mailer, email="bob@acme.dev")  # the address is free again


def test_ordinary_user_cannot_delete(client: TestClient, mailer: FakeMailer) -> None:
    headers = sign_up(client, mailer, email="bob@acme.dev")
    r = client.post(
        "/api/v1/admin/users/00000000-0000-0000-0000-000000000000/delete",
        json={"confirm_email": "x@acme.dev"}, headers=headers,
    )  # fmt: skip
    assert r.status_code == 404
