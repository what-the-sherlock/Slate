"""Content types: documents, chunks, parsed sections, search hits."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from core.domain.attachments import AttachmentInfo
from core.domain.ids import ChunkId, DocumentId, SourceId, WorkspaceId

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
    # An attachment's text (ADR-0043): the email/message it came with, in the same source.
    # The database copies that document's ACL and deletion onto this one.
    parent_external_id: str | None = None


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
    # The chunk's data class (ADR-0012), so answers know what they're built from.
    # Missing means private: fail closed.
    data_class: str = "private"
    # Document metadata the answer prompt shows (ADR-0031): when it was last updated or
    # sent (the upload time when the source gives none), who wrote it, where it lives,
    # and which connector it came from.
    updated_at: datetime | None = None
    author: str | None = None
    uri: str | None = None
    connector: str | None = None
    # ADR-0043: set on an attachment's chunks (the email/message it came with), and the
    # Tier 0 list of a document's own attachments (``metadata["attachments"]``).
    parent_id: DocumentId | None = None
    attachments: tuple[AttachmentInfo, ...] = ()


@dataclass(frozen=True, slots=True)
class SearchFilters:
    source_ids: tuple[SourceId, ...] = ()
    # Connector names (``sources.connector``), e.g. ("imap", "gmail_mbox") for email. Set
    # by the query planner when a question names a kind of source (ADR-0031).
    connectors: tuple[str, ...] = ()
    updated_after: datetime | None = None
    updated_before: datetime | None = None
    # Who wrote it: matched case-insensitively against the document's author (for email,
    # the people who sent messages in the thread; for GitHub, the issue author). M7 filters.
    author: str | None = None
    # ADR-0043 (vague questions): ``sent`` True = conversations the account sent (Sent
    # folder), False = ones it received; ``person`` matches anyone in the conversation
    # (sender, recipients) or the author, case-insensitively. An attachment matches
    # through its parent.
    sent: bool | None = None
    person: str | None = None

    @property
    def narrowing(self) -> bool:
        return bool(
            self.source_ids or self.connectors or self.author or self.person
            or self.sent is not None or self.updated_after or self.updated_before
        )  # fmt: skip


@dataclass(frozen=True, slots=True)
class Section:
    heading_path: str | None
    text: str


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    title: str | None
    sections: tuple[Section, ...]


@dataclass(frozen=True, slots=True)
class PreviewChunk:
    ordinal: int
    content: str
    heading_path: str | None = None


@dataclass(frozen=True, slots=True)
class DocumentPreview:
    """A document as the preview panel shows it: header plus its chunks in order."""

    id: DocumentId
    source_id: SourceId
    source_name: str
    title: str
    uri: str
    mime: str
    source_updated_at: datetime | None
    chunks: tuple[PreviewChunk, ...]


@dataclass(frozen=True, slots=True)
class ChunkToEmbed:
    """A stored chunk that still needs a vector in some generation (backfill)."""

    chunk_id: ChunkId
    workspace_id: WorkspaceId
    title: str
    heading_path: str | None
    content: str
    ordinal: int


@dataclass(frozen=True, slots=True)
class AttachmentParent:
    """A document whose attachments may be read on demand (ADR-0043)."""

    id: DocumentId
    workspace_id: WorkspaceId
    source_id: SourceId
    connector: str
    external_id: str
    title: str
    uri: str
    author: str | None
    updated_at: datetime | None
    data_class: str
    acl: tuple[str, ...]
    attachments: tuple[AttachmentInfo, ...]
