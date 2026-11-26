from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

from tests.fakes.auth import FakeMailer
from tests.unit.api.accounts import csrf, sign_up


def _token(mailer: FakeMailer) -> str:
    invite = next(m for m in reversed(mailer.sent) if "/invite#" in m.text)
    return invite.text.split("/invite#", 1)[1].split()[0]


def test_invite_accept_and_workspace_list(client: TestClient, mailer: FakeMailer) -> None:
    sign_up(client, mailer, email="owner@acme.dev")
    mine = client.get("/api/v1/workspaces").json()
    assert [w["active"] for w in mine] == [True]
    r = client.post(
        "/api/v1/workspaces/invites", json={"email": "bob@acme.dev", "role": "member"},
        headers=csrf(client),
    )  # fmt: skip
    assert r.status_code == 201, r.text
    assert r.json()["emailed"] is True
    token = _token(mailer)
    assert token not in r.text or r.json()["link"].endswith(token)  # only the inviter sees it
    client.cookies.clear()
    sign_up(client, mailer, email="bob@acme.dev")
    r = client.post("/api/v1/invites/accept", json={"token": token}, headers=csrf(client))
    assert r.status_code == 200, r.text
    spaces = client.get("/api/v1/workspaces").json()
    assert len(spaces) == 2
    active = next(w for w in spaces if w["active"])
    assert active["id"] == r.json()["workspace_id"]
    members = client.get("/api/v1/workspaces/members").json()
    assert {m["email"] for m in members} == {"owner@acme.dev", "bob@acme.dev"}
    # Back to their own workspace.
    own = next(w for w in spaces if not w["active"])
    assert (
        client.post(
            "/api/v1/workspaces/switch", json={"workspace_id": own["id"]}, headers=csrf(client)
        ).status_code
        == 204
    )
    assert (
        next(w for w in client.get("/api/v1/workspaces").json() if w["active"])["id"] == own["id"]
    )


def test_wrong_account_cannot_accept(client: TestClient, mailer: FakeMailer) -> None:
    sign_up(client, mailer, email="owner@acme.dev")
    client.post(
        "/api/v1/workspaces/invites", json={"email": "bob@acme.dev", "role": "member"},
        headers=csrf(client),
    )  # fmt: skip
    token = _token(mailer)
    client.cookies.clear()
    sign_up(client, mailer, email="mallory@evil.dev")
    r = client.post("/api/v1/invites/accept", json={"token": token}, headers=csrf(client))
    assert r.status_code == 403


def test_switching_to_a_foreign_workspace_is_404(client: TestClient, mailer: FakeMailer) -> None:
    sign_up(client, mailer)
    r = client.post(
        "/api/v1/workspaces/switch", json={"workspace_id": str(uuid4())}, headers=csrf(client)
    )
    assert r.status_code == 404


def test_sharing_someone_elses_or_missing_source_is_404(
    client: TestClient, mailer: FakeMailer
) -> None:
    sign_up(client, mailer)
    r = client.put(
        f"/api/v1/sources/{uuid4()}/sharing", json={"shared": True}, headers=csrf(client)
    )
    assert r.status_code == 404


def test_mutations_need_csrf(client: TestClient, mailer: FakeMailer) -> None:
    sign_up(client, mailer)
    assert client.post("/api/v1/workspaces/invites", json={"email": "a@b.dev"}).status_code == 403
    assert client.post("/api/v1/invites/accept", json={"token": "x" * 30}).status_code == 403
