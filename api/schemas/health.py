from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    version: str = Field(examples=["0.1.0"])


class ReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    database: bool
    migrations_current: bool
    migration_revision: str | None = Field(examples=["0001"])
    expected_revision: str | None = Field(examples=["0001"])
