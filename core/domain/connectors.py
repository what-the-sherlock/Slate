"""Connector output types (ARCHITECTURE §6). Connectors only fetch: they yield these."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class SourceDocument:
    external_id: str
    uri: str
    title: str
    mime: str
    updated_at: datetime
    author: str | None = None
    acl: tuple[str, ...] | None = None  # None -> inherit the source's visibility
    metadata: dict[str, object] = field(default_factory=dict[str, object])
    content: bytes | str | None = None  # small items inline
    blob_ref: str | None = None  # large items staged in the BlobStore


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """Opaque, JSON-serialisable resume point saved after each batch."""

    state: dict[str, object]


@dataclass(frozen=True, slots=True)
class CheckResult:
    ok: bool
    message: str


@dataclass(frozen=True, slots=True)
class RateLimit:
    requests: int
    per_seconds: float
