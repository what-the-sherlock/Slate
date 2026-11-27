"""Cited chat over SSE (ADR-0010: streams unbuffered through the same-origin rewrite).

``prepare`` runs before the stream opens, so validation, rate limits, budget and chat
ownership fail as ordinary problem+json responses. Once streaming, a failure becomes an
``error`` event with the same problem fields and the request id. The handler never logs
the question or the answer.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Query, Request, status
from sse_starlette.sse import EventSourceResponse

from api.deps import Container, get_container
from api.errors import STATUS_BY_ERROR, TYPE_BASE
from api.schemas.chat import (
    AnswerSourceOut,
    ChatBody,
    ChatEvents,
    ChatSessionOut,
    CitationEvent,
    CitationOut,
    DoneEvent,
    ErrorEvent,
    FeedbackBody,
    MessageOut,
    MetaEvent,
    SourcesEvent,
    TokenEvent,
)
from api.security import CurrentIdentity, MutatingIdentity
from core.domain.answer import Source
from core.domain.documents import SearchFilters
from core.domain.errors import AppError, UpstreamUnavailable
from core.domain.ids import ChatSessionId, MessageId, SourceId
from core.services.answer import AnswerEvent, AnswerService, Cited, Done, Meta, SourcesFound, Token

router = APIRouter(tags=["chat"])
log = structlog.get_logger(__name__)
SNIPPET_CHARS = 1200


def _answers(container: Annotated[Container, Depends(get_container)]) -> AnswerService:
    if container.answers is None:
        raise UpstreamUnavailable("Chat isn't configured on this server.")
    return container.answers


Answers = Annotated[AnswerService, Depends(_answers)]


def _source(s: Source) -> AnswerSourceOut:
    p = s.passage
    return AnswerSourceOut(
        n=s.n,
        chunk_id=int(p.chunk_id),
        document_id=p.document_id,
        ordinal=p.ordinal,
        title=p.title,
        heading_path=p.heading_path,
        snippet=p.content[:SNIPPET_CHARS],
    )


def _sse(event: AnswerEvent) -> dict[str, str]:
    match event:
        case Meta():
            body: Any = MetaEvent(
                session_id=event.session_id, user_message_id=event.user_message_id
            )
            name = "meta"
        case SourcesFound():
            body, name = SourcesEvent(sources=[_source(s) for s in event.sources]), "sources"
        case Token():
            body, name = TokenEvent(text=event.text), "token"
        case Cited():
            body = CitationEvent(
                n=event.n, chunk_id=int(event.chunk_id), document_id=event.document_id
            )
            name = "citation"
        case Done():
            body = DoneEvent(
                message_id=event.message_id,
                abstained=event.abstained,
                model=event.model,
                latency_ms=event.latency_ms,
                citations=list(event.citations),
                closest=[_source(s) for s in event.closest],
                notices=list(event.notices),
                mode=event.mode,
            )
            name = "done"
    return {"event": name, "data": body.model_dump_json()}


def _error_event(exc: Exception, request_id: str | None) -> dict[str, str]:
    if isinstance(exc, AppError):
        code = next((s for c, s in STATUS_BY_ERROR.items() if isinstance(exc, c)), 500)
        body = ErrorEvent(
            type=f"{TYPE_BASE}{exc.code}", title=exc.title, status=code, detail=exc.detail,
            request_id=request_id,
        )  # fmt: skip
    else:
        body = ErrorEvent(
            type=f"{TYPE_BASE}internal", title="Internal error", status=500,
            detail="The answer stopped before it finished.", request_id=request_id,
        )  # fmt: skip
    return {"event": "error", "data": body.model_dump_json()}


@router.post(
    "/chat",
    summary="Ask a question; streams a cited answer as server-sent events",
    response_class=EventSourceResponse,
    responses={
        200: {
            "model": ChatEvents,
            "description": "SSE stream; each data is JSON. ChatEvents maps names to shapes.",
        }
    },
)
async def chat(
    body: ChatBody, request: Request, identity: MutatingIdentity, answers: Answers
) -> EventSourceResponse:
    replace_from = MessageId(body.replace_from) if body.replace_from else None
    question, session = await answers.prepare(
        identity,
        body.question,
        ChatSessionId(body.session_id) if body.session_id else None,
        replace_from,
    )
    filters = SearchFilters(
        source_ids=tuple(SourceId(s) for s in body.source_ids),
        updated_after=body.updated_after,
        author=(body.author or "").strip() or None,
    )
    request_id: str | None = getattr(request.state, "request_id", None)

    async def events() -> AsyncIterator[dict[str, str]]:
        try:
            async for event in answers.answer(
                identity,
                question,
                session,
                filters,
                replace_from=replace_from,
                timezone=body.timezone,
            ):
                yield _sse(event)
        except Exception as exc:  # the stream has started: report in-band, never crash
            log.warning("chat.stream_failed", error=type(exc).__name__)
            yield _error_event(exc, request_id)

    return EventSourceResponse(
        events(),
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        ping=15,
    )


@router.get(
    "/chat/sessions", summary="Your chats, newest first", response_model=list[ChatSessionOut]
)
async def sessions(
    identity: CurrentIdentity,
    container: Annotated[Container, Depends(get_container)],
    before: Annotated[
        datetime | None, Query(description="Cursor: created_at of the last chat seen")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> list[ChatSessionOut]:
    if container.chats is None:
        raise UpstreamUnavailable("Chat isn't configured on this server.")
    found = await container.chats.list_sessions(
        identity.workspace_id, identity.user_id, limit=limit, before=before
    )
    return [ChatSessionOut(id=s.id, title=s.title, created_at=s.created_at) for s in found]


@router.get(
    "/chat/sessions/{session_id}/messages",
    summary="A chat's messages with their citations",
    response_model=list[MessageOut],
)
async def messages(
    session_id: UUID,
    identity: CurrentIdentity,
    container: Annotated[Container, Depends(get_container)],
) -> list[MessageOut]:
    if container.chats is None:
        raise UpstreamUnavailable("Chat isn't configured on this server.")
    stored = await container.chats.messages(
        identity.workspace_id, identity.user_id, ChatSessionId(session_id), limit=200
    )
    return [
        MessageOut(
            id=m.id,
            role="assistant" if m.role.value == "assistant" else "user",
            content=m.content,
            created_at=m.created_at,
            model=m.model,
            latency_ms=m.latency_ms,
            abstained=m.abstained,
            mode=m.mode,
            citations=[
                CitationOut(n=c.n, chunk_id=c.chunk_id, document_id=c.document_id)
                for c in m.citations
            ],
        )
        for m in stored
        if m.role.value in {"user", "assistant"}
    ]


@router.post(
    "/messages/{message_id}/feedback",
    summary="Rate an answer",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def feedback(
    message_id: UUID, body: FeedbackBody, identity: MutatingIdentity, answers: Answers
) -> None:
    await answers.feedback(
        identity,
        MessageId(message_id),
        rating=body.rating,
        reason=body.reason,
        comment=body.comment,
    )
