"""Chat request bodies, history responses and the versioned SSE event contract.

Every SSE event's ``data`` is one JSON object, described by the ``*Event`` models below
(ARCHITECTURE §8). The UI must render only what these events validate: a ``[n]`` in the
text becomes a citation badge only after a ``citation`` event for that n.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from core.domain.answer import MAX_QUESTION_CHARS, AnswerMode
from core.services.search import MAX_SOURCE_FILTERS

SSE_VERSION = 1


class ChatBody(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    session_id: UUID | None = Field(
        default=None, description="Continue this chat; omit to start one"
    )
    source_ids: list[UUID] = Field(default_factory=list[UUID], max_length=MAX_SOURCE_FILTERS)
    updated_after: datetime | None = Field(default=None, description="Only sources updated since")
    author: str | None = Field(
        default=None, max_length=200, description="Only sources by this person (name or email)"
    )
    replace_from: UUID | None = Field(
        default=None,
        description="Regenerate or edit: this question (a user message id in session_id) and"
        " everything after it are replaced by the new turn (ADR-0024)",
    )


class FeedbackBody(BaseModel):
    rating: Literal[1, -1]
    reason: Literal["Wrong", "Missing information", "Bad sources", "Other"] | None = None
    comment: str | None = Field(default=None, max_length=1000)


# ---------------------------------------------------------------- SSE events


class AnswerSourceOut(BaseModel):
    n: int
    chunk_id: int
    document_id: UUID
    ordinal: int
    title: str
    heading_path: str | None
    snippet: str = Field(description="The cited passage (may include neighbouring text)")


class MetaEvent(BaseModel):
    v: Literal[1] = SSE_VERSION
    session_id: UUID
    user_message_id: UUID


class SourcesEvent(BaseModel):
    v: Literal[1] = SSE_VERSION
    sources: list[AnswerSourceOut]


class TokenEvent(BaseModel):
    v: Literal[1] = SSE_VERSION
    text: str


class CitationEvent(BaseModel):
    v: Literal[1] = SSE_VERSION
    n: int
    chunk_id: int
    document_id: UUID


class DoneEvent(BaseModel):
    v: Literal[1] = SSE_VERSION
    message_id: UUID
    abstained: bool = Field(description="True: show the 'Not enough evidence' state")
    model: str | None
    latency_ms: int
    citations: list[int]
    closest: list[AnswerSourceOut] = Field(description="Closest matches, when abstaining")
    notices: list[str]
    mode: AnswerMode = Field(
        default="grounded",
        description="grounded: cited from sources; general: from general knowledge, not your"
        " sources (label it); chat: small talk (ADR-0025)",
    )


class ErrorEvent(BaseModel):
    v: Literal[1] = SSE_VERSION
    type: str
    title: str
    status: int
    detail: str
    request_id: str | None


class ChatEvents(BaseModel):
    """Documentation only: the SSE stream, as (event name -> data shape)."""

    meta: MetaEvent
    sources: SourcesEvent
    token: TokenEvent
    citation: CitationEvent
    done: DoneEvent
    error: ErrorEvent


# ---------------------------------------------------------------- history


class ChatSessionOut(BaseModel):
    id: UUID
    title: str | None
    created_at: datetime


class CitationOut(BaseModel):
    n: int
    chunk_id: int | None
    document_id: UUID | None


class MessageOut(BaseModel):
    id: UUID
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime
    model: str | None
    latency_ms: int | None
    abstained: bool
    mode: AnswerMode = "grounded"
    citations: list[CitationOut]
