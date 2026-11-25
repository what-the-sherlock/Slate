from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from api.security import CSRF_HEADER, MutatingIdentity, SessionMutatingIdentity, require_role
from core.domain.auth import Identity, Role
from tests.fakes.auth import FakeHumanVerifier, FakeMailer
from tests.unit.api.accounts import (
    CSRF_COOKIE,
    PASSWORD,
    SAME_SITE,
    SESSION_COOKIE,
    csrf,
    sign_up,
    signup_body,
)


@pytest.fixture
def protected_client(app: FastAPI) -> TestClient:
    """The app plus throwaway routes, to test the guards themselves."""

    @app.post("/test/mutate")
    async def mutate(identity: MutatingIdentity) -> dict[str, str]:
        return {"ok": identity.email}

    @app.post("/test/session")
    async def session(identity: SessionMutatingIdentity) -> dict[str, str]:
        return {"ok": identity.email}

    @app.post("/test/admin")
    async def admin(
        identity: Annotated[Identity, Depends(require_role(Role.ADMIN))],
    ) -> dict[str, bool]:
        return {"ok": True}

    return TestClient(app, raise_server_exceptions=False)


def test_config_tells_the_ui_whether_signup_is_open(client: TestClient) -> None:
    assert client.get("/api/v1/auth/config").json() == {
        "signups_enabled": True,
        "turnstile_site_key": None,
    }


def test_signup_sets_httponly_session_and_readable_csrf_cookie(client: TestClient) -> None:
    r = client.post("/api/v1/auth/signup", json=signup_body(), headers=SAME_SITE)
    assert r.status_code == 201, r.text
    cookies = r.headers.get_list("set-cookie")
    session = next(c for c in cookies if c.startswith(f"{SESSION_COOKIE}="))
    token = next(c for c in cookies if c.startswith(f"{CSRF_COOKIE}="))
    assert "HttpOnly" in session
    assert "SameSite=lax" in session
    assert "HttpOnly" not in token
    body = r.json()
    assert body["role"] == "owner"  # of their own, new workspace
    assert body["email_verified"] is False
    assert body["is_platform_admin"] is False


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/auth/signup",
        "/api/v1/auth/login",
        "/api/v1/auth/password/forgot",
        "/api/v1/auth/password/reset",
    ],
)
def test_pre_login_posts_require_the_same_site_header(client: TestClient, path: str) -> None:
    r = client.post(path, json=signup_body())
    assert r.status_code == 403


def test_removed_routes_are_gone(client: TestClient) -> None:
    for path in ("/api/v1/auth/setup", "/api/v1/auth/guest"):
        assert client.post(path, json={}, headers=SAME_SITE).status_code in {404, 405}
    assert client.get("/api/v1/auth/status").status_code == 404


def test_login_rejects_a_text_plain_body(client: TestClient, mailer: FakeMailer) -> None:
    # A cross-site <form> can only send text/plain; JSON content type is required.
    sign_up(client, mailer)
    client.cookies.clear()
    r = client.post(
        "/api/v1/auth/login",
        content=f'{{"email": "ada@acme.dev", "password": "{PASSWORD}"}}',
        headers={**SAME_SITE, "content-type": "text/plain"},
    )
    assert r.status_code == 422


def test_duplicate_signup_is_a_conflict_on_the_email_field(
    client: TestClient, mailer: FakeMailer
) -> None:
    sign_up(client, mailer)
    client.cookies.clear()
    r = client.post(
        "/api/v1/auth/signup", json=signup_body("A.D.A+x@acme.dev".lower()), headers=SAME_SITE
    )
    # "a.d.a" isn't Gmail, so dots matter: this is a different inbox. The +tag isn't.
    assert r.status_code == 201
    client.cookies.clear()
    r = client.post("/api/v1/auth/signup", json=signup_body("ada+2@acme.dev"), headers=SAME_SITE)
    assert r.status_code == 409
    assert "email" in r.json()["fields"]


def test_signup_validation_errors_list_fields(client: TestClient) -> None:
    r = client.post("/api/v1/auth/signup", json=signup_body(password="short"), headers=SAME_SITE)
    assert r.status_code == 422
    assert "password" in r.json()["fields"]


def test_signup_without_a_human_check_is_refused(client: TestClient) -> None:
    r = client.post("/api/v1/auth/signup", json=signup_body(turnstile_token=""), headers=SAME_SITE)
    assert r.status_code == 403


def test_signup_honeypot_is_refused(client: TestClient) -> None:
    r = client.post(
        "/api/v1/auth/signup", json=signup_body(website="http://spam"), headers=SAME_SITE
    )
    assert r.status_code == 422
    assert "fields" not in r.json()


def test_signup_human_check_outage_is_503_not_open(
    client: TestClient, human: FakeHumanVerifier
) -> None:
    human.down = True
    r = client.post("/api/v1/auth/signup", json=signup_body(), headers=SAME_SITE)
    assert r.status_code == 503


def test_signup_is_rate_limited_per_ip(client: TestClient) -> None:
    for n in range(3):
        client.cookies.clear()
        r = client.post(
            "/api/v1/auth/signup", json=signup_body(f"u{n}@acme.dev"), headers=SAME_SITE
        )
        assert r.status_code == 201, r.text
    client.cookies.clear()
    r = client.post("/api/v1/auth/signup", json=signup_body("u9@acme.dev"), headers=SAME_SITE)
    assert r.status_code == 429
    assert r.headers["retry-after"] == "3600"


def test_verify_with_the_emailed_code(client: TestClient, mailer: FakeMailer) -> None:
    headers = sign_up(client, mailer, verify=False)
    r = client.post("/api/v1/auth/verify", json={"code": "000000"}, headers=headers)
    if mailer.last_code() != "000000":
        assert r.status_code == 422
        assert "code" in r.json()["fields"]
    r = client.post("/api/v1/auth/verify", json={"code": mailer.last_code()}, headers=headers)
    assert r.status_code == 200
    assert r.json()["email_verified"] is True
    assert client.get("/api/v1/me").json()["email_verified"] is True


def test_verify_and_resend_need_csrf(client: TestClient, mailer: FakeMailer) -> None:
    sign_up(client, mailer, verify=False)
    assert client.post("/api/v1/auth/verify", json={"code": mailer.last_code()}).status_code == 403
    assert client.post("/api/v1/auth/verify/resend", json={}).status_code == 403


def test_resend_sends_a_new_code(client: TestClient, mailer: FakeMailer) -> None:
    headers = sign_up(client, mailer, verify=False)
    r = client.post(
        "/api/v1/auth/verify/resend",
        json={"turnstile_token": FakeHumanVerifier.PASS},
        headers=headers,
    )
    assert r.status_code == 204
    assert len(mailer.sent) == 2


def test_unverified_accounts_pass_session_guards_but_not_mutating_ones(
    protected_client: TestClient, mailer: FakeMailer
) -> None:
    headers = sign_up(protected_client, mailer, verify=False)
    assert protected_client.post("/test/session", headers=headers).status_code == 200
    r = protected_client.post("/test/mutate", headers=headers)
    assert r.status_code == 403
    assert r.json()["reason"] == "email_unverified"
    assert protected_client.post("/test/admin", headers=headers).status_code == 403


def test_verified_owner_passes_mutating_and_role_guards(
    protected_client: TestClient, mailer: FakeMailer
) -> None:
    headers = sign_up(protected_client, mailer)
    assert protected_client.post("/test/mutate", headers=headers).status_code == 200
    assert protected_client.post("/test/admin", headers=headers).status_code == 200


def test_me_requires_a_session(client: TestClient) -> None:
    r = client.get("/api/v1/me")
    assert r.status_code == 401
    assert r.headers["content-type"] == "application/problem+json"


def test_login_wrong_password_is_generic_401(client: TestClient, mailer: FakeMailer) -> None:
    sign_up(client, mailer)
    client.cookies.clear()
    r = client.post(
        "/api/v1/auth/login",
        json={"email": "ada@acme.dev", "password": "nope nope nope"},
        headers=SAME_SITE,
    )
    assert r.status_code == 401
    assert r.json()["detail"] == "Email or password is incorrect."


def test_login_is_throttled_with_retry_after(client: TestClient, mailer: FakeMailer) -> None:
    sign_up(client, mailer)
    client.cookies.clear()
    bad = {"email": "ada@acme.dev", "password": "nope nope nope"}
    for _ in range(5):
        client.post("/api/v1/auth/login", json=bad, headers=SAME_SITE)
    r = client.post("/api/v1/auth/login", json=bad, headers=SAME_SITE)
    assert r.status_code == 429
    assert r.headers["retry-after"] == "900"


def test_login_then_me(client: TestClient, mailer: FakeMailer) -> None:
    sign_up(client, mailer)
    client.cookies.clear()
    r = client.post(
        "/api/v1/auth/login",
        json={"email": "ADA@acme.dev", "password": PASSWORD},
        headers=SAME_SITE,
    )
    assert r.status_code == 200
    me = client.get("/api/v1/me").json()
    assert me["user"]["email"] == "ada@acme.dev"
    assert me["email_verified"] is True


def test_forgot_password_answers_the_same_for_any_email(
    client: TestClient, mailer: FakeMailer
) -> None:
    sign_up(client, mailer)
    client.cookies.clear()
    sent = len(mailer.sent)
    for email in ("ada@acme.dev", "nobody@acme.dev"):
        r = client.post(
            "/api/v1/auth/password/forgot",
            json={"email": email, "turnstile_token": FakeHumanVerifier.PASS},
            headers=SAME_SITE,
        )
        assert r.status_code == 202
        assert r.content in {b"", b"null"}
    assert len(mailer.sent) == sent + 1  # only the real account got mail


def test_password_reset_signs_out_every_session(client: TestClient, mailer: FakeMailer) -> None:
    sign_up(client, mailer)
    old_session = client.cookies.get(SESSION_COOKIE)
    client.post(
        "/api/v1/auth/password/forgot",
        json={"email": "ada@acme.dev", "turnstile_token": FakeHumanVerifier.PASS},
        headers=SAME_SITE,
    )
    r = client.post(
        "/api/v1/auth/password/reset",
        json={
            "email": "ada@acme.dev",
            "code": mailer.last_code(),
            "password": "a new long password",
        },
        headers=SAME_SITE,
    )
    assert r.status_code == 204
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE, old_session or "")
    assert client.get("/api/v1/me").status_code == 401
    client.cookies.clear()
    r = client.post(
        "/api/v1/auth/login",
        json={"email": "ada@acme.dev", "password": "a new long password"},
        headers=SAME_SITE,
    )
    assert r.status_code == 200


def test_logout_requires_csrf_header(client: TestClient, mailer: FakeMailer) -> None:
    sign_up(client, mailer)
    assert client.post("/api/v1/auth/logout").status_code == 403
    assert client.post("/api/v1/auth/logout", headers={CSRF_HEADER: "forged"}).status_code == 403
    assert client.get("/api/v1/me").status_code == 200  # still signed in


def test_csrf_header_matching_a_planted_cookie_is_still_rejected(
    client: TestClient, mailer: FakeMailer
) -> None:
    # An attacker who can plant a cookie value still doesn't know the session's secret.
    sign_up(client, mailer)
    client.cookies.set(CSRF_COOKIE, "attacker-chosen")
    r = client.post("/api/v1/auth/logout", headers={CSRF_HEADER: "attacker-chosen"})
    assert r.status_code == 403


def test_logout_with_csrf_ends_the_session(client: TestClient, mailer: FakeMailer) -> None:
    sign_up(client, mailer)
    assert client.post("/api/v1/auth/logout", headers=csrf(client)).status_code == 204
    assert client.get("/api/v1/me").status_code == 401
