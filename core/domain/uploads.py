"""Upload rules (pure): allowed types, size limits, content sniffing, visibility.

Types are decided from the file's *bytes*, never trusted from the name or the
browser-supplied Content-Type (SECURITY.md: "MIME sniffed from bytes").
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath

from core.domain.data_class import DataClass
from core.domain.ids import UserId, WorkspaceId
from core.domain.principals import PUBLIC, user_principal, workspace_principal

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
# Per workspace (ADR-0021): open sign-up must not fill Neon's 1 GB database or 5 GB
# bucket. Counted over live uploaded documents.
MAX_DOCUMENTS_PER_WORKSPACE = 50
MAX_STORED_BYTES_PER_WORKSPACE = 100 * 1024 * 1024
SNIFF_BYTES = 8192
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class FileKind(StrEnum):
    PDF = "pdf"
    DOCX = "docx"
    MARKDOWN = "md"
    TEXT = "txt"
    HTML = "html"
    CSV = "csv"


MIME: dict[FileKind, str] = {
    FileKind.PDF: "application/pdf",
    FileKind.DOCX: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    FileKind.MARKDOWN: "text/markdown",
    FileKind.TEXT: "text/plain",
    FileKind.HTML: "text/html",
    FileKind.CSV: "text/csv",
}
_BY_EXTENSION: dict[str, FileKind] = {
    ".pdf": FileKind.PDF,
    ".docx": FileKind.DOCX,
    ".md": FileKind.MARKDOWN,
    ".markdown": FileKind.MARKDOWN,
    ".txt": FileKind.TEXT,
    ".text": FileKind.TEXT,
    ".html": FileKind.HTML,
    ".htm": FileKind.HTML,
    ".csv": FileKind.CSV,
}
_TEXT_KINDS = {FileKind.MARKDOWN, FileKind.TEXT, FileKind.HTML, FileKind.CSV}

ALLOWED_DESCRIPTION = "PDF, Word (.docx), Markdown, HTML, CSV or plain text"


class Visibility(StrEnum):
    ONLY_ME = "only_me"
    WORKSPACE = "workspace"
    PUBLIC = "public"  # guests can see it; owner/admin only


def kind_from_filename(filename: str) -> FileKind | None:
    return _BY_EXTENSION.get(PurePosixPath(filename.lower()).suffix)


def sniff_kind(head: bytes, claimed: FileKind) -> FileKind | None:
    """Confirm the claimed kind from the first bytes, or None if they don't match.

    Binary formats are recognised by signature. Text formats must be UTF-8 (or plain
    ASCII) with no NUL bytes; a truncated multi-byte character at the cut is allowed.
    """
    if claimed is FileKind.PDF:
        return claimed if head.lstrip(b"\x00\t\r\n ").startswith(b"%PDF-") else None
    if claimed is FileKind.DOCX:
        return claimed if head.startswith(b"PK\x03\x04") else None
    if b"\x00" in head:
        return None
    try:
        head.decode("utf-8")
    except UnicodeDecodeError as e:
        if e.start < len(head) - 3:  # a real decoding error, not a cut character
            return None
    return claimed


def safe_filename(filename: str) -> str:
    """Display name only (never used as a path): strip directories and control chars."""
    name = PurePosixPath(filename.replace("\\", "/")).name
    name = "".join(ch for ch in name if ch.isprintable()).strip()
    return name[:200] or "untitled"


def is_sha256(value: str) -> bool:
    return bool(_SHA256.match(value))


@dataclass(frozen=True, slots=True)
class AccessPlan:
    acl: tuple[str, ...]
    data_class: DataClass


def access_for(visibility: Visibility, user: UserId, workspace: WorkspaceId) -> AccessPlan:
    """Who may read an upload, and its data class (ADR-0012)."""
    match visibility:
        case Visibility.ONLY_ME:
            return AccessPlan((user_principal(user),), DataClass.PRIVATE)
        case Visibility.WORKSPACE:
            return AccessPlan((workspace_principal(workspace),), DataClass.PRIVATE)
        case Visibility.PUBLIC:
            return AccessPlan((PUBLIC, workspace_principal(workspace)), DataClass.PUBLIC)


def external_id_for(sha256: str, uploader: UserId) -> str:
    """Upload identity: same uploader + same bytes = same document (dedup)."""
    return f"upload:{uploader}:{sha256}"


def blob_key_for(workspace: WorkspaceId, uploader: UserId, sha256: str, kind: FileKind) -> str:
    """Content-addressed per uploader: re-uploading the same bytes overwrites the same object,
    and no member can obtain an upload URL for another member's object.
    """
    return f"uploads/{workspace}/{uploader}/{sha256}.{kind.value}"


def is_text(kind: FileKind) -> bool:
    return kind in _TEXT_KINDS
