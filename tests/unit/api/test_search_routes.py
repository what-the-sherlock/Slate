from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from core.domain.documents import DocumentUpsert, NewChunk
from core.domain.ids import SourceId, WorkspaceId
from core.domain.principals import PUBLIC, workspace_principal
from tests.fakes.auth import FakeMailer
from tests.fakes.storage import InMemoryIndex
from tests.unit.api.accounts import csrf, sign_up


async def seed(index: InMemoryIndex, ws: WorkspaceId) -> dict[str, UUID]:
    ids: dict[str, UUID] = {}
    source = SourceId(uuid4())
    for ext, body, acl in [
        ("handbook", "Paid leave is 25 days a year.", (PUBLIC,)),
        ("internal", "Paid leave cover rota is internal.", (workspace_principal(ws),)),
    ]:
        doc_id, _ = await index.write_document(
            ws,
            DocumentUpsert(source, ext, f"upload://{ext}", ext.title(), "text/plain", ext, acl),
            [NewChunk(0, body, 6, heading_path="Leave")],
            {},
            data_class="private",
        )
        ids[ext] = doc_id
    return ids


@pytest.fixture
async def world(client: TestClient, index: InMemoryIndex, mailer: FakeMailer) -> dict[str, UUID]:
    sign_up(client, mailer)
    ws = WorkspaceId(UUID(client.get("/api/v1/me").json()["workspace_id"]))
    return await seed(index, ws)


def test_search_returns_hits_and_lanes(client: TestClient, world: dict[str, UUID]) -> None:
    r = client.post("/api/v1/search", json={"query": "paid leave"}, headers=csrf(client))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["lanes"] == ["keyword"]
    assert {h["title"] for h in body["hits"]} == {"Handbook", "Internal"}
    assert body["hits"][0]["matched_by"] == ["keyword"]
    assert body["hits"][0]["heading_path"] == "Leave"


def test_search_requires_session_and_csrf(client: TestClient, world: dict[str, UUID]) -> None:
    assert client.post("/api/v1/search", json={"query": "leave"}).status_code == 403
    client.post("/api/v1/auth/logout", headers=csrf(client))
    client.cookies.clear()
    assert client.post("/api/v1/search", json={"query": "leave"}).status_code == 401


@pytest.mark.parametrize(
    "body",
    [
        {"query": ""},
        {"query": "x" * 501},
        {"query": "a", "limit": 51},
        {"query": "a", "source_ids": [str(uuid4()) for _ in range(21)]},
    ],
)
def test_search_validates_input(
    client: TestClient, world: dict[str, UUID], body: dict[str, object]
) -> None:
    r = client.post("/api/v1/search", json=body, headers=csrf(client))
    assert r.status_code == 422


def test_another_account_sees_nothing_from_this_workspace(
    client: TestClient, world: dict[str, UUID], mailer: FakeMailer
) -> None:
    # ADR-0021: every sign-up gets its own workspace. Even a PUBLIC document in Ada's
    # workspace is out of Bob's reach: search and preview are scoped by workspace first.
    client.post("/api/v1/auth/logout", headers=csrf(client))
    client.cookies.clear()
    sign_up(client, mailer, email="bob@acme.dev", name="Bob")
    r = client.post("/api/v1/search", json={"query": "paid leave"}, headers=csrf(client))
    assert r.status_code == 200, r.text
    assert r.json()["hits"] == []
    assert client.get(f"/api/v1/documents/{world['handbook']}").status_code == 404
    hidden = client.get(f"/api/v1/documents/{world['internal']}")
    missing = client.get(f"/api/v1/documents/{uuid4()}")
    assert hidden.status_code == missing.status_code == 404
    assert hidden.json()["type"] == missing.json()["type"]  # no existence oracle


def test_preview_returns_chunks(client: TestClient, world: dict[str, UUID]) -> None:
    r = client.get(f"/api/v1/documents/{world['internal']}")
    assert r.status_code == 200
    assert r.json()["chunks"] == [
        {"ordinal": 0, "heading_path": "Leave", "content": "Paid leave cover rota is internal."}
    ]


def test_unverified_accounts_cannot_search(client: TestClient, mailer: FakeMailer) -> None:
    headers = sign_up(client, mailer, verify=False)
    r = client.post("/api/v1/search", json={"query": "leave"}, headers=headers)
    assert r.status_code == 403
    assert r.json()["reason"] == "email_unverified"
