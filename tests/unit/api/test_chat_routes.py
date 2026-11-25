from __future__ import annotations

import json
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from core.domain.answer import NO_ANSWER
from core.domain.documents import DocumentUpsert, NewChunk
from core.domain.ids import SourceId, WorkspaceId
from core.domain.principals import PUBLIC
from tests.fakes.auth import FakeMailer
from tests.fakes.models import FakeChatModel
from tests.fakes.storage import InMemoryIndex
from tests.unit.api.accounts import csrf, sign_up

LEAVE = "Everyone gets 25 days of paid leave per year."
Event = tuple[str, dict[str, object]]


def events(body: str) -> list[Event]:
    """Parse an SSE body into (event, data) pairs, as a browser would."""
    out: list[Event] = []
    name, data = "message", ""
    for line in body.splitlines():
        if line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            data += line[5:].strip()
        elif line == "" and data:
            out.append((name, json.loads(data)))
            name, data = "message", ""
    if data:
        out.append((name, json.loads(data)))
    return out


async def seed(client: TestClient, index: InMemoryIndex) -> WorkspaceId:
    ws = WorkspaceId(UUID(client.get("/api/v1/me").json()["workspace_id"]))
    doc = DocumentUpsert(
        SourceId(uuid4()), "h", "upload://h", "Handbook", "text/plain", "h", (PUBLIC,)
    )
    await index.write_document(ws, doc, [NewChunk(0, LEAVE, 9)], {}, data_class="private")
    return ws


def ask(client: TestClient, headers: dict[str, str], question: str) -> list[Event]:
    r = client.post("/api/v1/chat", json={"question": question}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")
    return events(r.text)


async def test_chat_streams_the_sse_contract(
    client: TestClient, mailer: FakeMailer, index: InMemoryIndex
) -> None:
    headers = sign_up(client, mailer)
    await seed(client, index)
    got = ask(client, headers, "How much paid leave?")
    names = [n for n, _ in got]
    assert names[0] == "meta"
    assert names[1] == "sources"
    assert "token" in names
    assert "citation" in names
    assert names[-1] == "done"
    assert all(d["v"] == 1 for _, d in got)
    sources = got[1][1]["sources"]
    assert isinstance(sources, list)
    assert sources[0]["n"] == 1
    assert sources[0]["title"] == "Handbook"
    done = got[-1][1]
    assert done["abstained"] is False
    assert done["citations"] == [1]


async def test_history_lists_the_chat_and_its_cited_messages(
    client: TestClient, mailer: FakeMailer, index: InMemoryIndex
) -> None:
    headers = sign_up(client, mailer)
    await seed(client, index)
    session = ask(client, headers, "How much paid leave?")[0][1]["session_id"]
    chats = client.get("/api/v1/chat/sessions").json()
    assert [c["id"] for c in chats] == [session]
    assert chats[0]["title"] == "How much paid leave?"
    msgs = client.get(f"/api/v1/chat/sessions/{session}/messages").json()
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["citations"][0]["n"] == 1


async def test_abstention_arrives_as_done_without_tokens(
    client: TestClient, mailer: FakeMailer, index: InMemoryIndex, chat_model: FakeChatModel
) -> None:
    chat_model._reply = NO_ANSWER
    headers = sign_up(client, mailer)
    await seed(client, index)
    got = ask(client, headers, "Is paid leave unlimited?")
    assert "token" not in [n for n, _ in got]
    assert got[-1][1]["abstained"] is True


def test_chat_needs_a_verified_account_and_csrf(client: TestClient, mailer: FakeMailer) -> None:
    headers = sign_up(client, mailer, verify=False)
    r = client.post("/api/v1/chat", json={"question": "hi"}, headers=headers)
    assert r.status_code == 403
    assert r.json()["reason"] == "email_unverified"
    client.cookies.clear()
    sign_up(client, mailer, email="bob@acme.dev", name="Bob")
    assert client.post("/api/v1/chat", json={"question": "hi"}).status_code == 403  # no CSRF


def test_validation_fails_as_problem_json_before_streaming(
    client: TestClient, mailer: FakeMailer
) -> None:
    headers = sign_up(client, mailer)
    r = client.post("/api/v1/chat", json={"question": "x" * 4001}, headers=headers)
    assert r.status_code == 422
    assert r.headers["content-type"] == "application/problem+json"


def test_another_accounts_chat_is_not_found(client: TestClient, mailer: FakeMailer) -> None:
    headers = sign_up(client, mailer)
    body = {"question": "hi", "session_id": str(uuid4())}
    assert client.post("/api/v1/chat", json=body, headers=headers).status_code == 404
    assert client.get(f"/api/v1/chat/sessions/{uuid4()}/messages").json() == []


async def test_a_failure_mid_stream_becomes_an_error_event(
    client: TestClient, mailer: FakeMailer, index: InMemoryIndex, chat_model: FakeChatModel
) -> None:
    chat_model._fail_after = 1
    headers = sign_up(client, mailer)
    await seed(client, index)
    name, data = ask(client, headers, "How much paid leave?")[-1]
    assert name == "error"
    assert data["status"] == 503
    assert data["request_id"]


async def test_feedback_on_an_answer(
    client: TestClient, mailer: FakeMailer, index: InMemoryIndex
) -> None:
    headers = sign_up(client, mailer)
    await seed(client, index)
    message = ask(client, headers, "How much paid leave?")[-1][1]["message_id"]
    url = f"/api/v1/messages/{message}/feedback"
    assert (
        client.post(url, json={"rating": -1, "reason": "Wrong"}, headers=csrf(client)).status_code
        == 204
    )
    assert client.post(url, json={"rating": 3}, headers=csrf(client)).status_code == 422
    missing = f"/api/v1/messages/{uuid4()}/feedback"
    assert client.post(missing, json={"rating": 1}, headers=csrf(client)).status_code == 404
