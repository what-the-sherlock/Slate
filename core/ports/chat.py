"""Conversation storage: chat sessions, messages, citations and feedback (ARCHITECTURE §4).

Every method is scoped to (workspace, user): a chat session belongs to one user, and
nothing here takes a session id without also checking who owns it. Document *contents*
are never stored here; citations hold ids only, and showing them goes back through the
ACL-checked SearchIndex.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from core.domain.chat import Role
from core.domain.ids import ChatSessionId, ChunkId, DocumentId, MessageId, UserId, WorkspaceId


@dataclass(frozen=True, slots=True)
class ChatSessionInfo:
    id: ChatSessionId
    title: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Citation:
    n: int
    chunk_id: ChunkId | None  # None once the chunk was re-ingested (the document stays)
    document_id: DocumentId | None


@dataclass(frozen=True, slots=True)
class StoredMessage:
    id: MessageId
    role: Role
    content: str
    created_at: datetime
    model: str | None = None
    latency_ms: int | None = None
    citations: tuple[Citation, ...] = field(default=())
    abstained: bool = False


@dataclass(frozen=True, slots=True)
class NewMessage:
    role: Role
    content: str
    parent_id: MessageId | None = None
    model: str | None = None
    usage: dict[str, object] | None = None  # tokens and cost; also marks abstentions
    latency_ms: int | None = None


class ChatStore(Protocol):
    async def create_session(
        self, ws: WorkspaceId, user: UserId, title: str | None
    ) -> ChatSessionId: ...

    async def get_session(
        self, ws: WorkspaceId, user: UserId, session: ChatSessionId
    ) -> ChatSessionInfo | None:
        """The session if this user owns it in this workspace; None otherwise (no probing)."""
        ...

    async def list_sessions(
        self, ws: WorkspaceId, user: UserId, *, limit: int, before: datetime | None
    ) -> list[ChatSessionInfo]:
        """Newest first; ``before`` is the cursor (created_at of the last item seen)."""
        ...

    async def messages(
        self, ws: WorkspaceId, user: UserId, session: ChatSessionId, *, limit: int
    ) -> list[StoredMessage]:
        """The session's latest messages, oldest first, with citations. Empty if not owned."""
        ...

    async def add_message(
        self, ws: WorkspaceId, user: UserId, session: ChatSessionId, message: NewMessage
    ) -> MessageId:
        """Raises NotFound if the session isn't this user's."""
        ...

    async def add_citations(self, message: MessageId, citations: Sequence[Citation]) -> None: ...

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
        """Upsert this user's feedback on an assistant message in one of their sessions.
        Raises NotFound otherwise (same answer for missing and someone else's)."""
        ...
