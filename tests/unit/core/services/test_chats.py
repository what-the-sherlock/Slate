"""Chat rename, delete with Undo, and the purge (ADR-0044)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from core.domain.auth import Identity, Role
from core.domain.chat import MAX_TITLE_CHARS, clean_title
from core.domain.errors import NotFound, ValidationFailed
from core.domain.ids import SessionId, UserId, WorkspaceId
from core.domain.principals import Principals
from core.services.chats import UNDO_WINDOW, ChatHistory, purge_deleted_chats
from tests.fakes.chat import InMemoryChatStore
from tests.fakes.system import FakeClock

WS = WorkspaceId(uuid4())
NOW = datetime(2026, 11, 28, 12, 0, tzinfo=UTC)


def who(user: UserId | None = None) -> Identity:
    u = user or UserId(uuid4())
    return Identity(
        user_id=u, workspace_id=WS, session_id=SessionId(uuid4()), email="a@b.c", name="A",
        role=Role.OWNER, email_verified=True, principals=Principals.for_user(u, WS),
        csrf_secret="c",
    )  # fmt: skip


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("  Leave\n\tpolicy ", "Leave policy"),
        ("a\u0000b", "a b"),
        ("x" * 300, "x" * MAX_TITLE_CHARS),
    ],
)
def test_titles_are_one_clean_line(raw: str, clean: str) -> None:
    assert clean_title(raw) == clean


@pytest.mark.parametrize("raw", ["", "   ", "\u0007\u0008"])
def test_an_empty_title_is_refused(raw: str) -> None:
    with pytest.raises(ValidationFailed):
        clean_title(raw)


async def test_delete_undo_and_purge_follow_the_window() -> None:
    clock = FakeClock(NOW)
    chats = InMemoryChatStore(clock)
    me = who()
    history = ChatHistory(chats, clock)
    sid = await chats.create_session(WS, me.user_id, "t")
    await history.delete(me, sid)
    assert await chats.get_session(WS, me.user_id, sid) is None
    await history.restore(me, sid)
    assert await chats.get_session(WS, me.user_id, sid) is not None

    await history.delete(me, sid)
    clock._now = NOW + UNDO_WINDOW + timedelta(minutes=1)
    with pytest.raises(NotFound):  # too late to undo
        await history.restore(me, sid)
    assert await purge_deleted_chats(chats, clock) == 1
    assert sid not in chats.sessions


async def test_a_purge_keeps_live_chats_and_recent_deletes() -> None:
    clock = FakeClock(NOW)
    chats = InMemoryChatStore(clock)
    me = who()
    live = await chats.create_session(WS, me.user_id, "keep")
    recent = await chats.create_session(WS, me.user_id, "undo me")
    await ChatHistory(chats, clock).delete(me, recent)
    assert await purge_deleted_chats(chats, clock) == 0
    assert set(chats.sessions) == {live, recent}


async def test_only_the_owner_can_act_on_a_chat() -> None:
    clock = FakeClock(NOW)
    chats = InMemoryChatStore(clock)
    owner, other = who(), who()
    history = ChatHistory(chats, clock)
    sid = await chats.create_session(WS, owner.user_id, "mine")
    for action in (
        history.rename(other, sid, "x"),
        history.delete(other, sid),
        history.restore(other, sid),
    ):
        with pytest.raises(NotFound):
            await action
    assert (await chats.get_session(WS, owner.user_id, sid)) is not None
