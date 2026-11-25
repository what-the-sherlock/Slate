"""ChatStore over Postgres.

``messages`` has no workspace_id of its own (ADR-0012): every statement reaches it through
``chat_sessions`` and filters ``workspace_id = :ws AND user_id = :u`` there, so a guessed
session or message id from another account matches nothing.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import text

from core.adapters.postgres.uow import UnitOfWork
from core.domain.chat import Role
from core.domain.errors import NotFound
from core.domain.ids import ChatSessionId, ChunkId, DocumentId, MessageId, UserId, WorkspaceId
from core.ports.chat import ChatSessionInfo, Citation, NewMessage, StoredMessage

_OWNED = "s.workspace_id = :ws AND s.user_id = :u"
_GET_SESSION = (
    "SELECT s.id, s.title, s.created_at FROM chat_sessions s"  # noqa: S608 (constants only)
    f" WHERE s.id = :id AND {_OWNED}"
)
_MESSAGES = (
    "SELECT * FROM ("  # noqa: S608 (constants only)
    "  SELECT m.id, m.role, m.content, m.created_at, m.model, m.latency_ms, m.usage,"
    "   coalesce(json_agg(json_build_object('n', c.n, 'chunk', c.chunk_id,"
    "     'doc', c.document_id) ORDER BY c.n)"
    "     FILTER (WHERE c.n IS NOT NULL), '[]') AS cites"
    "  FROM messages m JOIN chat_sessions s ON s.id = m.session_id"
    "  LEFT JOIN message_citations c ON c.message_id = m.id"
    f"  WHERE m.session_id = :id AND {_OWNED}"
    "  GROUP BY m.id ORDER BY m.created_at DESC LIMIT :n"
    ") t ORDER BY created_at"
)


def _info(r: Any) -> ChatSessionInfo:  # reason: RowMapping values are Any
    return ChatSessionInfo(ChatSessionId(r["id"]), r["title"], r["created_at"])


class PostgresChatStore:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def create_session(
        self, ws: WorkspaceId, user: UserId, title: str | None
    ) -> ChatSessionId:
        async with self._uow() as s:
            row = (
                await s.execute(
                    text(
                        "INSERT INTO chat_sessions (workspace_id, user_id, title)"
                        " VALUES (:ws, :u, :t) RETURNING id"
                    ),
                    {"ws": ws, "u": user, "t": title},
                )
            ).scalar_one()
        return ChatSessionId(row)

    async def get_session(
        self, ws: WorkspaceId, user: UserId, session: ChatSessionId
    ) -> ChatSessionInfo | None:
        async with self._uow() as s:
            row = (
                (
                    await s.execute(
                        text(_GET_SESSION),
                        {"id": session, "ws": ws, "u": user},
                    )
                )
                .mappings()
                .first()
            )
        return None if row is None else _info(row)

    async def list_sessions(
        self, ws: WorkspaceId, user: UserId, *, limit: int, before: datetime | None
    ) -> list[ChatSessionInfo]:
        params: dict[str, Any] = {"ws": ws, "u": user, "n": max(1, min(limit, 100))}
        cursor = ""
        if before is not None:
            cursor = " AND s.created_at < :before"
            params["before"] = before
        async with self._uow() as s:
            rows = (
                (
                    await s.execute(
                        text(
                            "SELECT s.id, s.title, s.created_at FROM chat_sessions s"  # noqa: S608
                            f" WHERE {_OWNED}{cursor} ORDER BY s.created_at DESC LIMIT :n"
                        ),
                        params,
                    )
                )
                .mappings()
                .all()
            )
        return [_info(r) for r in rows]

    async def messages(
        self, ws: WorkspaceId, user: UserId, session: ChatSessionId, *, limit: int
    ) -> list[StoredMessage]:
        params = {"id": session, "ws": ws, "u": user, "n": max(1, min(limit, 200))}
        async with self._uow() as s:
            rows = (
                (
                    await s.execute(
                        text(_MESSAGES),
                        params,
                    )
                )
                .mappings()
                .all()
            )
        out: list[StoredMessage] = []
        for r in rows:
            usage: Any = r["usage"] or {}
            cites: Any = r["cites"]
            out.append(
                StoredMessage(
                    id=MessageId(r["id"]),
                    role=Role(r["role"]),
                    content=str(r["content"]),
                    created_at=r["created_at"],
                    model=r["model"],
                    latency_ms=r["latency_ms"],
                    abstained=bool(usage.get("abstained")),
                    citations=tuple(
                        Citation(
                            n=int(c["n"]),
                            chunk_id=ChunkId(int(c["chunk"])) if c["chunk"] is not None else None,
                            document_id=DocumentId(c["doc"]) if c["doc"] is not None else None,
                        )
                        for c in cites
                    ),
                )
            )
        return out

    async def add_message(
        self, ws: WorkspaceId, user: UserId, session: ChatSessionId, message: NewMessage
    ) -> MessageId:
        params = {
            "id": session,
            "ws": ws,
            "u": user,
            "parent": message.parent_id,
            "role": message.role.value,
            "content": message.content,
            "model": message.model,
            "usage": json.dumps(message.usage) if message.usage is not None else None,
            "latency": message.latency_ms,
        }
        async with self._uow() as s:
            row = (
                await s.execute(
                    text(
                        # INSERT ... SELECT: inserts nothing unless the session is owned.
                        "INSERT INTO messages (session_id, parent_id, role, content, model,"  # noqa: S608
                        " usage, latency_ms)"
                        " SELECT s.id, :parent, :role, :content, :model,"
                        " CAST(:usage AS jsonb), :latency"
                        f" FROM chat_sessions s WHERE s.id = :id AND {_OWNED} RETURNING id"
                    ),
                    params,
                )
            ).first()
        if row is None:
            raise NotFound("chat not found")
        return MessageId(row[0])

    async def add_citations(self, message: MessageId, citations: Sequence[Citation]) -> None:
        if not citations:
            return
        async with self._uow() as s:
            await s.execute(
                text(
                    "INSERT INTO message_citations (message_id, n, chunk_id, document_id)"
                    " SELECT :m, n, chunk, doc FROM unnest(CAST(:ns AS smallint[]),"
                    " CAST(:chunks AS bigint[]), CAST(:docs AS uuid[])) AS t(n, chunk, doc)"
                    " ON CONFLICT (message_id, n) DO NOTHING"
                ),
                {
                    "m": message,
                    "ns": [c.n for c in citations],
                    "chunks": [c.chunk_id for c in citations],
                    "docs": [c.document_id for c in citations],
                },
            )

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
        async with self._uow() as s:
            row = (
                await s.execute(
                    text(
                        "INSERT INTO feedback (message_id, user_id, rating, reason, comment)"  # noqa: S608
                        " SELECT m.id, :u, :rating, :reason, :comment FROM messages m"
                        " JOIN chat_sessions s ON s.id = m.session_id"
                        f" WHERE m.id = :m AND m.role = 'assistant' AND {_OWNED}"
                        " ON CONFLICT (message_id, user_id) DO UPDATE SET rating = excluded.rating,"
                        " reason = excluded.reason, comment = excluded.comment, created_at = now()"
                        " RETURNING message_id"
                    ),
                    {
                        "m": message,
                        "ws": ws,
                        "u": user,
                        "rating": rating,
                        "reason": reason,
                        "comment": comment,
                    },
                )
            ).first()
        if row is None:
            raise NotFound("message not found")
