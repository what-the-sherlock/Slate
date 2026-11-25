"""ChatStore over Postgres: ownership, history, citations and feedback (ADR-0022)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from sqlalchemy import text

from core.adapters.postgres.auth import PostgresAccountStore
from core.adapters.postgres.chat import PostgresChatStore
from core.adapters.postgres.uow import UnitOfWork
from core.domain.chat import Role
from core.domain.errors import NotFound
from core.domain.ids import ChatSessionId, UserId, WorkspaceId
from core.ports.chat import Citation, NewMessage


@pytest.fixture
async def people(
    uow: UnitOfWork,
) -> AsyncIterator[tuple[tuple[WorkspaceId, UserId], tuple[WorkspaceId, UserId]]]:
    tag = uuid4().hex[:10]
    store = PostgresAccountStore(uow)
    made = []
    for who in ("a", "b"):
        email = f"chat-{who}-{tag}@acme.dev"
        made.append(
            await store.create_user_with_workspace(
                email=email,
                email_canonical=email,
                name=who,
                password_hash="h",
                workspace_name="w",
                slug=f"chat-{who}-{tag}",
            )
        )
    yield made[0], made[1]
    async with uow() as s:
        await s.execute(text("DELETE FROM workspaces WHERE slug LIKE :p"), {"p": f"chat-%-{tag}"})
        await s.execute(
            text("DELETE FROM users WHERE email LIKE :p"), {"p": f"chat-%-{tag}@acme.dev"}
        )


async def test_history_citations_and_feedback_are_owned(
    uow: UnitOfWork, people: tuple[tuple[WorkspaceId, UserId], tuple[WorkspaceId, UserId]]
) -> None:
    (ws_a, a), (ws_b, b) = people
    chats = PostgresChatStore(uow)
    sid = await chats.create_session(ws_a, a, "Leave?")
    q = await chats.add_message(ws_a, a, sid, NewMessage(Role.USER, "How much leave?"))
    ans = await chats.add_message(
        ws_a, a, sid,
        NewMessage(Role.ASSISTANT, "25 days [1].", parent_id=q, model="m", usage={"cost_usd": "0.001"}, latency_ms=12),
    )  # fmt: skip
    await chats.add_citations(ans, [Citation(1, None, None)])
    msgs = await chats.messages(ws_a, a, sid, limit=10)
    assert [m.role for m in msgs] == [Role.USER, Role.ASSISTANT]
    assert [c.n for c in msgs[1].citations] == [1]
    assert [s.id for s in await chats.list_sessions(ws_a, a, limit=10, before=None)] == [sid]

    # Account B can't read, extend or rate A's chat, even with the ids.
    assert await chats.get_session(ws_b, b, sid) is None
    assert await chats.messages(ws_b, b, sid, limit=10) == []
    with pytest.raises(NotFound):
        await chats.add_message(ws_b, b, sid, NewMessage(Role.USER, "hi"))
    with pytest.raises(NotFound):
        await chats.save_feedback(ws_b, b, ans, rating=1, reason=None, comment=None)
    # Feedback only on assistant messages, and it upserts.
    with pytest.raises(NotFound):
        await chats.save_feedback(ws_a, a, q, rating=1, reason=None, comment=None)
    await chats.save_feedback(ws_a, a, ans, rating=1, reason=None, comment=None)
    await chats.save_feedback(ws_a, a, ans, rating=-1, reason="Wrong", comment="no")
    async with uow() as s:
        row = (
            await s.execute(
                text("SELECT rating, reason FROM feedback WHERE message_id = :m"), {"m": ans}
            )
        ).one()
    assert tuple(row) == (-1, "Wrong")


async def test_abstentions_are_marked_in_history(
    uow: UnitOfWork, people: tuple[tuple[WorkspaceId, UserId], tuple[WorkspaceId, UserId]]
) -> None:
    (ws, user), _ = people
    chats = PostgresChatStore(uow)
    sid = await chats.create_session(ws, user, None)
    await chats.add_message(
        ws, user, sid, NewMessage(Role.ASSISTANT, "x", usage={"abstained": True})
    )
    (msg,) = await chats.messages(ws, user, sid, limit=5)
    assert msg.abstained
    assert await chats.get_session(ws, user, ChatSessionId(uuid4())) is None


async def test_supersede_retires_the_turn_and_later_ones_only_for_the_owner(
    uow: UnitOfWork, people: tuple[tuple[WorkspaceId, UserId], tuple[WorkspaceId, UserId]]
) -> None:
    (ws_a, a), (ws_b, b) = people
    chats = PostgresChatStore(uow)
    sid = await chats.create_session(ws_a, a, "Leave?")
    q1 = await chats.add_message(ws_a, a, sid, NewMessage(Role.USER, "How much leave?"))
    a1 = await chats.add_message(ws_a, a, sid, NewMessage(Role.ASSISTANT, "25 days.", parent_id=q1))
    q2 = await chats.add_message(ws_a, a, sid, NewMessage(Role.USER, "Part-timers?"))
    await chats.add_message(ws_a, a, sid, NewMessage(Role.ASSISTANT, "Pro rata.", parent_id=q2))

    # Another account can't supersede A's messages, and nothing changes when it tries.
    with pytest.raises(NotFound):
        await chats.add_message(ws_b, b, sid, NewMessage(Role.USER, "x"), supersede_from=q2)
    # Only a live user message can be the anchor.
    with pytest.raises(NotFound):
        await chats.add_message(ws_a, a, sid, NewMessage(Role.USER, "x"), supersede_from=a1)
    assert len(await chats.messages(ws_a, a, sid, limit=10)) == 4

    # Regenerate the second turn: it drops out, the first stays, the new question is live.
    new_q = await chats.add_message(
        ws_a, a, sid, NewMessage(Role.USER, "Part-timers?"), supersede_from=q2
    )
    live = await chats.messages(ws_a, a, sid, limit=10)
    assert [m.id for m in live] == [q1, a1, new_q]
    with pytest.raises(NotFound):  # already superseded: can't be an anchor again
        await chats.add_message(ws_a, a, sid, NewMessage(Role.USER, "x"), supersede_from=q2)

    # Edit the first question: everything from it on is retired.
    edited = await chats.add_message(
        ws_a, a, sid, NewMessage(Role.USER, "Sick leave?"), supersede_from=q1
    )
    assert [m.id for m in await chats.messages(ws_a, a, sid, limit=10)] == [edited]
    async with uow() as s:
        kept = (
            await s.execute(text("SELECT count(*) FROM messages WHERE session_id = :s"), {"s": sid})
        ).scalar_one()
    assert kept == 6  # nothing deleted: superseded rows stay for feedback and audit
