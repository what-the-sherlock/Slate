from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class BeginUploadBody(BaseModel):
    filename: str = Field(min_length=1, max_length=255, examples=["handbook.pdf"])
    size: int = Field(ge=0, description="Bytes, as reported by the browser.", examples=[183244])
    sha256: str = Field(
        min_length=64,
        max_length=64,
        description="Hex SHA-256 of the file, computed in the browser.",
    )
    visibility: Literal["only_me", "workspace"] = "only_me"


class PresignedPost(BaseModel):
    url: str
    fields: dict[str, str] = Field(description="Send these form fields first, then the file.")


UploadStatusName = Literal["upload", "duplicate", "processing", "visibility_differs", "indexed"]


class BeginUploadResponse(BaseModel):
    status: UploadStatusName
    filename: str
    document_id: UUID | None = None
    upload: PresignedPost | None = None
    upload_token: str | None = None


class CompleteUploadBody(BaseModel):
    upload_token: str = Field(min_length=10, max_length=4096)


class CompleteUploadResponse(BaseModel):
    status: UploadStatusName
    job_id: UUID | None = None
    document_id: UUID | None = None
