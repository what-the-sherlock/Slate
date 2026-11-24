"""Content types: documents, chunks, parsed sections, search hits."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from core.domain.ids import ChunkId, DocumentId, SourceId

Vector = Sequence[float]


@dataclass(frozen=True, slots=True)
class DocumentUpsert:
    """What ingestion writes. Identity is ``(source_id, external_id)``."""

    source_id: SourceId
    external_id: str
    uri: str
    title: str
    mime: str
    checksum: str
    acl: tuple[str, ...]
    author: str | None = None
    source_created_at: datetime | None = None
    source_updated_at: datetime | None = None
    metadata: Mapping[str, object] = field(default_factory=dict[str, object])


@dataclass(frozen=True, slots=True)
class Document:
    id: DocumentId
    source_id: SourceId
    external_id: str
    uri: str
    title: str
    mime: str
    checksum: str
    version: int
    acl: tuple[str, ...]
    author: str | None = None
    source_updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class NewChunk:
    ordinal: int
    content: str
    token_count: int
    heading_path: str | None = None


@dataclass(frozen=True, slots=True)
class ScoredChunk:
    chunk_id: ChunkId
    document_id: DocumentId
    ordinal: int
    content: str
    score: float
    heading_path: str | None = None
    title: str | None = None  # the document's title, for result lists


@dataclass(frozen=True, slots=True)
class SearchFilters:
    source_ids: tuple[SourceId, ...] = ()
    updated_after: datetime | None = None
    updated_before: datetime | None = None


@dataclass(frozen=True, slots=True)
class Section:
    heading_path: str | None
    text: str


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    title: str | None
    sections: tuple[Section, ...]
