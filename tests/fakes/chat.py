from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime

from core.domain.errors import NotFound
from core.domain.ids import ChatSessionId, MessageId, UserId, WorkspaceId
from core.ports.chat import ChatSessionInfo, Citation, NewMessage, StoredMessage
from tests.fakes.system import FakeClock, SequentialIds


@dataclass
class _Session:
    ws: WorkspaceId
    user: UserId
    info: ChatSessionInfo
    messages: list[StoredMessage] = field(default_factory=list[StoredMessage])


class InMemoryChatStore:
    """Same ownership rules as Postgres: everything is scoped to (workspace, user)."""

    def __init__(self, clock: FakeClock | None = None) -> None:
        self._clock = clock or FakeClock()
        self._ids = SequentialIds()
        self.sessions: dict[ChatSessionId, _Session] = {}
        self.feedback: dict[tuple[MessageId, UserId], tuple[int, str | None, str | None]] = {}
        self.usage: dict[MessageId, dict[str, object] | None] = {}

    def _owned(self, ws: WorkspaceId, user: UserId, sid: ChatSessionId) -> _Session | None:
        s = self.sessions.get(sid)
        return s if s is not None and s.ws == ws and s.user == user else None

    async def create_session(
        self, ws: WorkspaceId, user: UserId, title: str | None
    ) -> ChatSessionId:
        sid = ChatSessionId(self._ids.new_uuid())
        self.sessions[sid] = _Session(ws, user, ChatSessionInfo(sid, title, self._clock.now()))
        return sid

    async def get_session(
        self, ws: WorkspaceId, user: UserId, session: ChatSessionId
    ) -> ChatSessionInfo | None:
        s = self._owned(ws, user, session)
        return None if s is None else s.info

    async def list_sessions(
        self, ws: WorkspaceId, user: UserId, *, limit: int, before: datetime | None
    ) -> list[ChatSessionInfo]:
        mine = [
            s.info
            for s in self.sessions.values()
            if s.ws == ws and s.user == user and (before is None or s.info.created_at < before)
        ]
        return sorted(mine, key=lambda i: i.created_at, reverse=True)[:limit]

    async def messages(
        self, ws: WorkspaceId, user: UserId, session: ChatSessionId, *, limit: int
    ) -> list[StoredMessage]:
        s = self._owned(ws, user, session)
        return [] if s is None else s.messages[-limit:]

    async def add_message(
        self, ws: WorkspaceId, user: UserId, session: ChatSessionId, message: NewMessage
    ) -> MessageId:
        s = self._owned(ws, user, session)
        if s is None:
            raise NotFound("chat not found")
        mid = MessageId(self._ids.new_uuid())
        abstained = bool((message.usage or {}).get("abstained"))
        s.messages.append(
            StoredMessage(
                mid,
                message.role,
                message.content,
                self._clock.now(),
                message.model,
                message.latency_ms,
                abstained=abstained,
            )
        )
        self.usage[mid] = message.usage
        return mid

    async def add_citations(self, message: MessageId, citations: Sequence[Citation]) -> None:
        for s in self.sessions.values():
            for i, m in enumerate(s.messages):
                if m.id == message:
                    s.messages[i] = replace(m, citations=(*m.citations, *citations))

    async def save_feedback(
        self,
        ws: WorkspaceId,
        user: UserId,
        message: MessageId,
        *,
        rating: int,
        reason: str | None,
        comment: str | None,
    ) -> None:
        for s in self.sessions.values():
            if s.ws != ws or s.user != user:
                continue
            if any(m.id == message and m.role.value == "assistant" for m in s.messages):
                self.feedback[(message, user)] = (rating, reason, comment)
                return
        raise NotFound("message not found")
