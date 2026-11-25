from __future__ import annotations

from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from fastapi.testclient import TestClient

from core.domain.github import Repo
from tests.fakes.auth import FakeMailer
from tests.fakes.github import FakeGitHub, FakeRepo
from tests.unit.api.accounts import csrf, sign_up

INSTALL = 7


def setup_repo(gh: FakeGitHub) -> None:
    gh.add_repo(
        INSTALL, FakeRepo(Repo(1, "octo/docs", True, "main", "https://github.com/octo/docs"))
    )
    gh.owners["code-ok"] = {INSTALL}


def grant(client: TestClient) -> str:
    r = client.post("/api/v1/sources/github/install", headers=csrf(client))
    assert r.status_code == 200, r.text
    state = parse_qs(urlsplit(r.json()["url"]).query)["state"][0]
    r = client.post(
        "/api/v1/sources/github/installations",
        json={"installation_id": INSTALL, "code": "code-ok", "state": state},
        headers=csrf(client),
    )
    assert r.status_code == 200, r.text
    return r.json()["grant"]


def test_status_says_whether_github_is_available(
    client: TestClient, mailer: FakeMailer, fake_github: FakeGitHub
) -> None:
    sign_up(client, mailer)
    assert client.get("/api/v1/sources/github/status").json() == {"available": True}
    fake_github._configured = False
    assert client.get("/api/v1/sources/github/status").json() == {"available": False}
    assert client.post("/api/v1/sources/github/install", headers=csrf(client)).status_code == 503


def test_full_connect_flow(client: TestClient, mailer: FakeMailer, fake_github: FakeGitHub) -> None:
    setup_repo(fake_github)
    sign_up(client, mailer)
    g = grant(client)
    repos = client.post(
        "/api/v1/sources/github/repos", json={"grant": g}, headers=csrf(client)
    ).json()
    assert [(r["full_name"], r["connected"], r["private"]) for r in repos] == [
        ("octo/docs", False, True)
    ]
    r = client.post(
        "/api/v1/sources/github/connect",
        json={"grant": g, "repos": ["octo/docs"]},
        headers=csrf(client),
    )
    assert r.status_code == 201, r.text
    (connected,) = r.json()
    assert connected["name"] == "octo/docs"
    repos = client.post(
        "/api/v1/sources/github/repos", json={"grant": g}, headers=csrf(client)
    ).json()
    assert repos[0]["connected"] is True


def test_someone_elses_installation_is_refused(
    client: TestClient, mailer: FakeMailer, fake_github: FakeGitHub
) -> None:
    setup_repo(fake_github)
    sign_up(client, mailer)
    r = client.post("/api/v1/sources/github/install", headers=csrf(client))
    state = parse_qs(urlsplit(r.json()["url"]).query)["state"][0]
    r = client.post(
        "/api/v1/sources/github/installations",
        json={"installation_id": INSTALL, "code": "not-the-owner", "state": state},
        headers=csrf(client),
    )
    assert r.status_code == 403


def test_github_routes_need_a_verified_account_and_csrf(
    client: TestClient, mailer: FakeMailer
) -> None:
    headers = sign_up(client, mailer, verify=False)
    assert client.post("/api/v1/sources/github/install", headers=headers).status_code == 403
    client.cookies.clear()
    sign_up(client, mailer, email="bob@acme.dev", name="Bob")
    assert client.post("/api/v1/sources/github/install").status_code == 403  # no CSRF


def test_sync_now_and_disconnect(
    client: TestClient, mailer: FakeMailer, fake_github: FakeGitHub
) -> None:
    setup_repo(fake_github)
    sign_up(client, mailer)
    g = grant(client)
    (c,) = client.post(
        "/api/v1/sources/github/connect",
        json={"grant": g, "repos": ["octo/docs"]},
        headers=csrf(client),
    ).json()
    r = client.post(f"/api/v1/sources/{c['source_id']}/sync", headers=csrf(client))
    assert r.status_code == 200, r.text
    assert r.json()["job_id"] == c["job_id"]  # the waiting initial sync, not a second job
    assert r.json()["status"] == "queued"  # no inline runner in this test container
    assert (
        client.delete(f"/api/v1/sources/{c['source_id']}", headers=csrf(client)).status_code == 204
    )
    assert (
        client.delete(f"/api/v1/sources/{c['source_id']}", headers=csrf(client)).status_code == 404
    )
    assert client.post(f"/api/v1/sources/{uuid4()}/sync", headers=csrf(client)).status_code == 404
