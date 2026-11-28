"""ChatStore over Postgres.

``messages`` has no workspace_id of its own (ADR-0012): every statement reaches it through
``chat_sessions`` and filters ``workspace_id = :ws AND user_id = :u`` there, so a guessed
session or message id from another account matches nothing.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast

from sqlalchemy import text

from core.adapters.postgres.uow import UnitOfWork
from core.domain.answer import AnswerMode
from core.domain.chat import Role
from core.domain.errors import NotFound
from core.domain.ids import ChatSessionId, ChunkId, DocumentId, MessageId, UserId, WorkspaceId
from core.ports.chat import ChatSessionInfo, Citation, NewMessage, StoredMessage

# A deleted chat (ADR-0044) is gone for every read and write through this filter.
_OWNED = "s.workspace_id = :ws AND s.user_id = :u AND s.deleted_at IS NULL"
_GET_SESSION = (
    "SELECT s.id, s.title, s.created_at FROM chat_sessions s"  # noqa: S608 (constants only)
    f" WHERE s.id = :id AND {_OWNED}"
)
# Regenerate/edit (ADR-0024): retire the anchor user message and everything after it.
# The anchor must be a live user message of a session this user owns.
_SUPERSEDE = (
    "UPDATE messages m SET superseded_at = now()"  # noqa: S608 (constants only)
    " FROM messages anchor JOIN chat_sessions s ON s.id = anchor.session_id"
    " WHERE anchor.id = :anchor AND anchor.session_id = :id AND anchor.role = 'user'"
    f" AND anchor.superseded_at IS NULL AND {_OWNED}"
    " AND m.session_id = anchor.session_id AND m.superseded_at IS NULL"
    " AND m.created_at >= anchor.created_at RETURNING m.id"
)
_MESSAGES = (
    "SELECT * FROM ("  # noqa: S608 (constants only)
    "  SELECT m.id, m.role, m.content, m.created_at, m.model, m.latency_ms, m.usage,"
    "   coalesce(json_agg(json_build_object('n', c.n, 'chunk', c.chunk_id,"
    "     'doc', c.document_id) ORDER BY c.n)"
    "     FILTER (WHERE c.n IS NOT NULL), '[]') AS cites"
    "  FROM messages m JOIN chat_sessions s ON s.id = m.session_id"
    "  LEFT JOIN message_citations c ON c.message_id = m.id"
    f"  WHERE m.session_id = :id AND m.superseded_at IS NULL AND {_OWNED}"
    "  GROUP BY m.id ORDER BY m.created_at DESC LIMIT :n"
    ") t ORDER BY created_at"
)


def _mode(raw: object) -> AnswerMode:
    return raw if raw in ("general", "chat") else "grounded"


def _followups(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(q for q in cast("list[object]", raw) if isinstance(q, str))[:3]


def _info(r: Any) -> ChatSessionInfo:  # reason: RowMapping values are Any
    return ChatSessionInfo(ChatSessionId(r["id"]), r["title"], r["created_at"])


class _SessionEdits:
    """Rename / delete / restore / purge (ADR-0044); mixed into PostgresChatStore."""

    _uow: UnitOfWork

    async def _changed(self, sql: str, params: dict[str, Any]) -> bool:
        async with self._uow() as s:
            return (await s.execute(text(sql), params)).first() is not None

    async def rename_session(
        self, ws: WorkspaceId, user: UserId, session: ChatSessionId, title: str
    ) -> bool:
        return await self._changed(
            f"UPDATE chat_sessions s SET title = :t WHERE s.id = :id AND {_OWNED}"  # noqa: S608
            " RETURNING s.id",
            {"t": title, "id": session, "ws": ws, "u": user},
        )

    async def delete_session(self, ws: WorkspaceId, user: UserId, session: ChatSessionId) -> bool:
        return await self._changed(
            f"UPDATE chat_sessions s SET deleted_at = now() WHERE s.id = :id AND {_OWNED}"  # noqa: S608
            " RETURNING s.id",
            {"id": session, "ws": ws, "u": user},
        )

    async def restore_session(
        self, ws: WorkspaceId, user: UserId, session: ChatSessionId, *, since: datetime
    ) -> bool:
        return await self._changed(
            "UPDATE chat_sessions s SET deleted_at = NULL WHERE s.id = :id"
            " AND s.workspace_id = :ws AND s.user_id = :u AND s.deleted_at >= :since"
            " RETURNING s.id",
            {"id": session, "ws": ws, "u": user, "since": since},
        )

    async def purge_deleted(self, before: datetime) -> int:
        async with self._uow() as s:
            rows = await s.execute(
                text("DELETE FROM chat_sessions WHERE deleted_at < :b RETURNING id"), {"b": before}
            )
            return len(rows.all())


class PostgresChatStore(_SessionEdits):
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
                    mode=_mode(usage.get("mode")),
                    followups=_followups(usage.get("followups")),
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
        self,
        ws: WorkspaceId,
        user: UserId,
        session: ChatSessionId,
        message: NewMessage,
        *,
        supersede_from: MessageId | None = None,
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
            if supersede_from is not None:
                retired = (
                    await s.execute(
                        text(_SUPERSEDE),
                        {"anchor": supersede_from, "id": session, "ws": ws, "u": user},
                    )
                ).all()
                if not retired:
                    raise NotFound("message not found")  # rolls the transaction back
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
