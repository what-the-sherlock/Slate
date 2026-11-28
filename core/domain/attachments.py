"""Attachment rules (ADR-0043), pure: no I/O.

An attachment moves through tiers:

* **Tier 0, reference** (``AttachmentInfo``): name, type, size, a provider ``ref`` and a
  ``state``. Always stored, in the parent's ``metadata["attachments"]``.
* **Tier 1, text:** a child document whose chunks are keyword-searchable, not embedded.
* **Tier 2, vectors:** embeddings, written lazily once someone's question reaches it.

``state`` is what the answer uses to say *why* a file can't be read, instead of guessing.
"""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import PurePosixPath
from typing import cast

from core.domain.uploads import MIME, FileKind, kind_from_filename

MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024  # per file; bigger ones are listed as too_large
MAX_ATTACHMENTS_READ = 50  # per message, beyond Tier 0 (memory is bounded per page/run)
MAX_ATTACHMENTS_LISTED = 50  # per message, Tier 0
MAX_BYTES_PER_PAGE = 30 * 1024 * 1024  # attachment bytes one sync page may hold in memory
MAX_ATTACHMENT_TOKENS = 60_000  # text indexed per file (~120 pages); the rest is truncated
PARSE_TIMEOUT_S = 20.0
MAX_NAME_CHARS = 200
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # encrypted Office files are OLE, not zip

# Types worth reading (the upload parsers' types, minus mbox).
READABLE: frozenset[FileKind] = frozenset(
    {FileKind.PDF, FileKind.DOCX, FileKind.MARKDOWN, FileKind.TEXT, FileKind.HTML, FileKind.CSV}
)
_MIME_KIND: dict[str, FileKind] = {m: k for k, m in MIME.items() if k in READABLE}
_MIME_KIND["application/msword"] = FileKind.DOCX  # often mislabelled; the sniff decides


class AttachmentState(StrEnum):
    LISTED = "listed"  # known, not read yet (on demand, or beyond the per-message limit)
    TEXT = "text"  # Tier 1 (and later Tier 2): contents indexed in a child document
    UNSUPPORTED = "unsupported"  # images, archives, spreadsheets...
    TOO_LARGE = "too_large"
    ENCRYPTED = "encrypted"
    UNREADABLE = "unreadable"  # damaged, or not what its name claims
    EMPTY = "empty"  # no text layer (a scan)
    GONE = "gone"  # deleted at the provider, or no longer accessible
    DENIED_BY_BUDGET = "denied_by_budget"


# States a read can still change: anything else is final for the same bytes.
RETRYABLE = frozenset({AttachmentState.LISTED, AttachmentState.DENIED_BY_BUDGET})
INDEXED = frozenset({AttachmentState.TEXT})

_EXPLAIN: dict[AttachmentState, str] = {
    AttachmentState.LISTED: "not read yet",
    AttachmentState.TEXT: "contents indexed",
    AttachmentState.UNSUPPORTED: "this file type can't be read",
    AttachmentState.TOO_LARGE: "too large to read (over 10 MB)",
    AttachmentState.ENCRYPTED: "password-protected, so it can't be read",
    AttachmentState.UNREADABLE: "damaged, or not the type its name says",
    AttachmentState.EMPTY: "no text inside (it may be a scanned image)",
    AttachmentState.GONE: "no longer available at the source",
    AttachmentState.DENIED_BY_BUDGET: "not read: the monthly AI budget is used up",
}


@dataclass(frozen=True, slots=True)
class AttachmentInfo:
    """Tier 0. ``key`` is stable for the same message and part (Message-ID hash + part
    number), so a thread gaining replies keeps its children; ``ref`` is what the connector
    needs to fetch the file again."""

    key: str
    name: str
    mime: str
    size: int
    state: AttachmentState = AttachmentState.LISTED
    ref: Mapping[str, str] = field(default_factory=dict[str, str])
    truncated: bool = False

    def to_meta(self) -> dict[str, object]:
        out: dict[str, object] = {
            "key": self.key, "name": self.name, "mime": self.mime, "size": self.size,
            "state": self.state.value, "ref": dict(self.ref),
        }  # fmt: skip
        if self.truncated:
            out["truncated"] = True
        return out

    def with_state(self, state: AttachmentState, *, truncated: bool = False) -> AttachmentInfo:
        return replace(self, state=state, truncated=truncated)


@dataclass(frozen=True, slots=True)
class AttachmentBlob:
    """Tier 0 plus the bytes, carried from a connector to the engine (rule 2: connectors
    only fetch; the engine sniffs and parses)."""

    info: AttachmentInfo
    data: bytes = field(repr=False)


def clean_name(raw: str | None, fallback: str = "attachment") -> str:
    """A display name: last path part, NFC, printable characters only, bounded."""
    name = unicodedata.normalize("NFC", str(raw or ""))
    name = PurePosixPath(name.replace("\\", "/")).name
    name = "".join(ch for ch in name if ch.isprintable()).strip()
    return name[:MAX_NAME_CHARS] or fallback


def claimed_kind(name: str, mime: str) -> FileKind | None:
    """What the file says it is (extension first, then declared type); the bytes must
    still confirm it (``sniff_kind``)."""
    by_name = kind_from_filename(name)
    if by_name is not None:
        return by_name if by_name in READABLE else None
    return _MIME_KIND.get(mime.split(";", 1)[0].strip().lower())


def looks_encrypted(head: bytes, kind: FileKind) -> bool:
    """A password-protected .docx is an OLE container, not a zip."""
    return kind is FileKind.DOCX and head.startswith(_OLE_MAGIC)


def key_seed(message_id: str) -> str:
    """The message part of an email attachment's key. A file fetched again must come from
    a message with the same Message-ID (an IMAP UID can point elsewhere after a mailbox
    is rebuilt), or it is treated as gone."""
    return hashlib.sha1(message_id.encode(), usedforsecurity=False).hexdigest()[:12]


def child_external_id(parent_external_id: str, key: str) -> str:
    return f"{parent_external_id}#att/{key}"


def is_valid_key(key: str) -> bool:
    return 0 < len(key) <= 64 and all(ch.isalnum() or ch in "-_" for ch in key)


def human_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size // 1024} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def kind_label(name: str, mime: str) -> str:
    kind = kind_from_filename(name)
    if kind is not None:
        return kind.value.upper()
    suffix = PurePosixPath(name.lower()).suffix.lstrip(".")
    return (suffix or mime.split("/")[-1] or "file").upper()[:10]


def listing_line(info: AttachmentInfo) -> str:
    """The line in the parent's text. Stable across states, so reading a file never
    changes (and re-embeds) the parent."""
    return (
        f"[attachment: {info.name} ({kind_label(info.name, info.mime)}, {human_size(info.size)})]"
    )


def explain(info: AttachmentInfo) -> str:
    """What the answer may say about a file it couldn't read."""
    return f"{info.name}: {_EXPLAIN[info.state]}"


def from_meta(raw: object) -> list[AttachmentInfo]:
    """The parent's ``metadata["attachments"]``, validated; bad entries are dropped."""
    if not isinstance(raw, list):
        return []
    out: list[AttachmentInfo] = []
    for value in cast(list[object], raw)[:MAX_ATTACHMENTS_LISTED]:
        if not isinstance(value, dict):
            continue
        item = cast(dict[str, object], value)
        try:
            key = str(item["key"])
            size = int(str(item.get("size") or 0))
            state = AttachmentState(str(item.get("state")))
        except (KeyError, TypeError, ValueError):
            continue
        if not is_valid_key(key):
            continue
        ref_raw = item.get("ref")
        ref = (
            {str(k): str(v) for k, v in cast(dict[object, object], ref_raw).items()}
            if isinstance(ref_raw, dict)
            else {}
        )
        out.append(
            AttachmentInfo(
                key=key,
                name=clean_name(str(item.get("name") or "")),
                mime=str(item.get("mime") or "application/octet-stream")[:100],
                size=max(size, 0),
                state=state,
                ref=ref,
                truncated=bool(item.get("truncated")),
            )
        )
    return out


def first_pass_state(info: AttachmentInfo, *, position: int) -> AttachmentState | None:
    """A final state decidable from metadata alone, or None (the bytes must be checked).
    ``position`` is the file's order among its message's attachments."""
    if position >= MAX_ATTACHMENTS_READ:
        return AttachmentState.LISTED
    if info.size > MAX_ATTACHMENT_BYTES:
        return AttachmentState.TOO_LARGE
    if info.mime == "message/rfc822":
        return None
    if claimed_kind(info.name, info.mime) is None:
        return AttachmentState.UNSUPPORTED
    return None


def mime_for(kind: FileKind) -> str:
    return MIME[kind]


def blob_digest(blobs: Sequence[AttachmentBlob]) -> str:
    """Folded into the parent's checksum, so a file replaced under the same name and size
    still re-indexes the conversation."""
    h = hashlib.sha256()
    for b in sorted(blobs, key=lambda b: b.info.key):
        h.update(b.info.key.encode() + b"\x00" + hashlib.sha256(b.data).digest())
    return h.hexdigest()
