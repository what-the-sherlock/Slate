from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from api.security import CSRF_HEADER, MutatingIdentity, require_role
from core.domain.auth import Identity, Role

SAME_SITE = {"x-requested-with": "slate"}
SETUP = {
    "workspace_name": "Acme",
    "name": "Ada",
    "email": "ada@acme.dev",
    "password": "correct horse battery",
}
SESSION_COOKIE, CSRF_COOKIE = "slate_session", "slate_csrf"  # dev names (ENV=dev in tests)


def setup(client: TestClient) -> None:
    r = client.post("/api/v1/auth/setup", json=SETUP, headers=SAME_SITE)
    assert r.status_code == 201, r.text


def csrf(client: TestClient) -> dict[str, str]:
    return {CSRF_HEADER: client.cookies.get(CSRF_COOKIE) or ""}


@pytest.fixture
def protected_client(app: FastAPI) -> TestClient:
    """The app plus two throwaway mutating routes, to test the guards themselves."""

    @app.post("/test/mutate")
    async def mutate(identity: MutatingIdentity) -> dict[str, str]:
        return {"ok": identity.email}

    @app.post("/test/admin")
    async def admin(
        identity: Annotated[Identity, Depends(require_role(Role.ADMIN))],
    ) -> dict[str, bool]:
        return {"ok": True}

    return TestClient(app, raise_server_exceptions=False)


def test_status_reports_setup(client: TestClient) -> None:
    assert client.get("/api/v1/auth/status").json() == {
        "set_up": False,
        "requires_setup_token": False,
    }
    setup(client)
    assert client.get("/api/v1/auth/status").json()["set_up"] is True


def test_setup_sets_httponly_session_and_readable_csrf_cookie(client: TestClient) -> None:
    r = client.post("/api/v1/auth/setup", json=SETUP, headers=SAME_SITE)
    cookies = r.headers.get_list("set-cookie")
    session = next(c for c in cookies if c.startswith(f"{SESSION_COOKIE}="))
    token = next(c for c in cookies if c.startswith(f"{CSRF_COOKIE}="))
    assert "HttpOnly" in session
    assert "SameSite=lax" in session
    assert "HttpOnly" not in token
    assert r.json()["role"] == "owner"


@pytest.mark.parametrize("path", ["/api/v1/auth/setup", "/api/v1/auth/login", "/api/v1/auth/guest"])
def test_pre_login_posts_require_the_same_site_header(client: TestClient, path: str) -> None:
    r = client.post(path, json=SETUP)
    assert r.status_code == 403


def test_login_rejects_a_text_plain_body(client: TestClient) -> None:
    # A cross-site <form> can only send text/plain; JSON content type is required.
    setup(client)
    client.cookies.clear()
    r = client.post(
        "/api/v1/auth/login",
        content='{"email": "ada@acme.dev", "password": "correct horse battery"}',
        headers={**SAME_SITE, "content-type": "text/plain"},
    )
    assert r.status_code == 422


def test_second_setup_is_a_conflict(client: TestClient) -> None:
    setup(client)
    assert client.post("/api/v1/auth/setup", json=SETUP, headers=SAME_SITE).status_code == 409


def test_setup_validation_errors_list_fields(client: TestClient) -> None:
    r = client.post("/api/v1/auth/setup", json={**SETUP, "password": "short"}, headers=SAME_SITE)
    assert r.status_code == 422
    assert "password" in r.json()["fields"]


def test_me_requires_a_session(client: TestClient) -> None:
    r = client.get("/api/v1/me")
    assert r.status_code == 401
    assert r.headers["content-type"] == "application/problem+json"


def test_login_wrong_password_is_generic_401(client: TestClient) -> None:
    setup(client)
    client.cookies.clear()
    r = client.post(
        "/api/v1/auth/login",
        json={"email": SETUP["email"], "password": "nope nope nope"},
        headers=SAME_SITE,
    )
    assert r.status_code == 401
    assert r.json()["detail"] == "Email or password is incorrect."


def test_login_is_throttled_with_retry_after(client: TestClient) -> None:
    setup(client)
    client.cookies.clear()
    bad = {"email": SETUP["email"], "password": "nope nope nope"}
    for _ in range(5):
        client.post("/api/v1/auth/login", json=bad, headers=SAME_SITE)
    r = client.post("/api/v1/auth/login", json=bad, headers=SAME_SITE)
    assert r.status_code == 429
    assert r.headers["retry-after"] == "900"


def test_login_then_me(client: TestClient) -> None:
    setup(client)
    client.cookies.clear()
    r = client.post(
        "/api/v1/auth/login",
        json={"email": "ADA@acme.dev", "password": SETUP["password"]},
        headers=SAME_SITE,
    )
    assert r.status_code == 200
    me = client.get("/api/v1/me").json()
    assert me["user"]["email"] == "ada@acme.dev"
    assert me["is_guest"] is False


def test_logout_requires_csrf_header(client: TestClient) -> None:
    setup(client)
    assert client.post("/api/v1/auth/logout").status_code == 403
    assert client.post("/api/v1/auth/logout", headers={CSRF_HEADER: "forged"}).status_code == 403
    assert client.get("/api/v1/me").status_code == 200  # still signed in


def test_csrf_header_matching_a_planted_cookie_is_still_rejected(client: TestClient) -> None:
    # An attacker who can plant a cookie value still doesn't know the session's secret.
    setup(client)
    client.cookies.set(CSRF_COOKIE, "attacker-chosen")
    r = client.post("/api/v1/auth/logout", headers={CSRF_HEADER: "attacker-chosen"})
    assert r.status_code == 403


def test_logout_with_csrf_ends_the_session(client: TestClient) -> None:
    setup(client)
    assert client.post("/api/v1/auth/logout", headers=csrf(client)).status_code == 204
    assert client.get("/api/v1/me").status_code == 401


def test_guest_session_is_read_only_viewer(client: TestClient) -> None:
    setup(client)
    client.cookies.clear()
    r = client.post("/api/v1/auth/guest", headers=SAME_SITE)
    assert r.status_code == 200
    assert r.json()["is_guest"] is True
    assert r.json()["role"] == "viewer"


def test_guests_are_refused_on_mutating_routes_by_default(protected_client: TestClient) -> None:
    setup(protected_client)
    protected_client.cookies.clear()
    protected_client.post("/api/v1/auth/guest", headers=SAME_SITE)
    r = protected_client.post("/test/mutate", headers=csrf(protected_client))
    assert r.status_code == 403
    assert "Guests can't make changes" in r.json()["detail"]
    assert protected_client.post("/test/admin", headers=csrf(protected_client)).status_code == 403


def test_owner_passes_mutating_and_role_guards(protected_client: TestClient) -> None:
    setup(protected_client)
    assert protected_client.post("/test/mutate", headers=csrf(protected_client)).status_code == 200
    assert protected_client.post("/test/admin", headers=csrf(protected_client)).status_code == 200
