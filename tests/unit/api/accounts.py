"""Signing up over HTTP in API tests (ADR-0021: there is no setup or guest route)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from api.security import CSRF_HEADER
from tests.fakes.auth import FakeHumanVerifier, FakeMailer

SAME_SITE = {"x-requested-with": "slate"}
PASSWORD = "correct horse battery"
SESSION_COOKIE, CSRF_COOKIE = "slate_session", "slate_csrf"  # dev names (ENV=dev in tests)


def signup_body(
    email: str = "ada@acme.dev", name: str = "Ada", **extra: object
) -> dict[str, object]:
    return {
        "name": name,
        "email": email,
        "password": PASSWORD,
        "turnstile_token": FakeHumanVerifier.PASS,
        **extra,
    }


def csrf(client: TestClient) -> dict[str, str]:
    return {CSRF_HEADER: client.cookies.get(CSRF_COOKIE) or ""}


def sign_up(
    client: TestClient,
    mailer: FakeMailer,
    *,
    email: str = "ada@acme.dev",
    name: str = "Ada",
    verify: bool = True,
) -> dict[str, str]:
    """Sign up (and by default verify) a fresh account; returns its CSRF header."""
    r = client.post("/api/v1/auth/signup", json=signup_body(email, name), headers=SAME_SITE)
    assert r.status_code == 201, r.text
    headers = csrf(client)
    if verify:
        r = client.post("/api/v1/auth/verify", json={"code": mailer.last_code()}, headers=headers)
        assert r.status_code == 200, r.text
    return headers
