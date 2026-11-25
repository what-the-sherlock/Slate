from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from api.security import CSRF_HEADER
from core.domain.documents import DocumentUpsert, NewChunk
from core.domain.ids import SourceId, WorkspaceId
from core.domain.principals import PUBLIC, workspace_principal
from tests.fakes.storage import InMemoryIndex

SAME_SITE = {"x-requested-with": "slate"}
SETUP = {
    "workspace_name": "Acme",
    "name": "Ada",
    "email": "ada@acme.dev",
    "password": "correct horse battery",
}


def csrf(client: TestClient) -> dict[str, str]:
    return {CSRF_HEADER: client.cookies.get("slate_csrf") or ""}


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
async def world(client: TestClient, index: InMemoryIndex) -> dict[str, UUID]:
    assert client.post("/api/v1/auth/setup", json=SETUP, headers=SAME_SITE).status_code == 201
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


def test_guest_search_and_preview_see_only_public(
    client: TestClient, world: dict[str, UUID]
) -> None:
    client.post("/api/v1/auth/logout", headers=csrf(client))
    client.cookies.clear()
    assert client.post("/api/v1/auth/guest", headers=SAME_SITE).status_code in {200, 201}
    r = client.post("/api/v1/search", json={"query": "paid leave"}, headers=csrf(client))
    assert {h["title"] for h in r.json()["hits"]} == {"Handbook"}
    assert client.get(f"/api/v1/documents/{world['handbook']}").status_code == 200
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
