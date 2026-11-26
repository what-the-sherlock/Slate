from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from core.domain.search import MAX_QUERY_CHARS
from core.services.search import MAX_LIMIT, MAX_SOURCE_FILTERS


class SearchBody(BaseModel):
    query: str = Field(min_length=1, max_length=MAX_QUERY_CHARS)
    limit: int = Field(default=10, ge=1, le=MAX_LIMIT)
    source_ids: list[UUID] = Field(default_factory=list[UUID], max_length=MAX_SOURCE_FILTERS)
    updated_after: datetime | None = None
    updated_before: datetime | None = None
    author: str | None = Field(
        default=None, max_length=200, description="Only documents by this person (name or email)"
    )


class SearchHitOut(BaseModel):
    chunk_id: int
    document_id: UUID
    title: str
    heading_path: str | None
    content: str
    ordinal: int
    score: float = Field(description="Reciprocal rank fusion score; only the order matters")
    matched_by: list[Literal["vector", "keyword"]] = Field(
        description="Which retrieval lanes found this passage"
    )


class SearchOut(BaseModel):
    hits: list[SearchHitOut]
    lanes: list[Literal["vector", "keyword"]] = Field(description="Lanes that actually ran")
    notices: list[str] = Field(description="Honest degradations to show the user")


class PreviewChunkOut(BaseModel):
    ordinal: int
    heading_path: str | None
    content: str


class DocumentPreviewOut(BaseModel):
    id: UUID
    source_id: UUID
    source_name: str
    title: str
    uri: str
    mime: str
    source_updated_at: datetime | None
    chunks: list[PreviewChunkOut]
