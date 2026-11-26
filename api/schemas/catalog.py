from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class SourceOut(BaseModel):
    id: UUID
    name: str
    connector: str
    visibility: Literal["only_me", "workspace", "public", "mixed"]
    data_class: Literal["public", "private"]
    status: Literal["synced", "syncing", "queued", "failed", "never", "paused", "partial"]
    documents: int
    last_synced_at: datetime | None
    last_error: str | None
    shareable: bool = Field(
        default=False, description="You connected it, so you may share it with the workspace"
    )


class RunErrorOut(BaseModel):
    stage: str
    message: str
    retryable: bool


class RunOut(BaseModel):
    id: UUID
    kind: str
    status: str
    started_at: datetime | None
    finished_at: datetime | None
    docs_seen: int
    docs_changed: int
    docs_failed: int
    error_summary: str | None
    errors: list[RunErrorOut]


class JobOut(BaseModel):
    id: UUID
    status: Literal["queued", "running", "succeeded", "failed", "dead"]
    attempts: int
    error: str | None
    filename: str | None


class ActivityOut(RunOut):
    source_id: UUID
    source_name: str
    connector: str


class SyncOut(BaseModel):
    job_id: UUID
    status: str = Field(description="done: synced now; queued: the worker finishes it")
