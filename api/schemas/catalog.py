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
    status: Literal[
        "synced", "syncing", "queued", "retrying", "waiting_for_worker", "failed", "partial",
        "empty", "never", "paused",
    ]  # fmt: skip
    status_reason: str | None = Field(
        default=None, description="Why the source is in this state and what to do, if anything"
    )
    busy: bool = Field(default=False, description="Work is moving (counts as processing)")
    healthy: bool = Field(default=True, description="False: something needs attention")
    documents: int
    last_synced_at: datetime | None
    last_error: str | None
    skipped_summary: str | None = Field(
        default=None,
        description="What the last run left out and why, e.g. '30 PDFs (not supported yet)'",
    )
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
    docs_skipped: int = 0
    skipped: dict[str, int] = Field(default_factory=dict[str, int])
    skipped_summary: str | None = None


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
    status: Literal["done", "partial", "failed", "background"] = Field(
        description=(
            "done: synced now; partial: part synced, see reason; failed: it didn't work, see"
            " reason; background: the worker does it"
        )
    )
    reason: str | None = Field(default=None, description="Why it's partial or failed")
    skipped_summary: str | None = Field(
        default=None, description="What the sync left out and why, e.g. '12 code files (...)'"
    )
