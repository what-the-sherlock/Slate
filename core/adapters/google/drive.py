"""Google Drive over the Drive API with an OAuth token (ADR-0033). Read-only
(``drive.readonly``; see ADR-0033 for why not ``drive.file``).

Indexed (P1-1), not in the trash, newest-modified first:
* Google Docs and Slides, exported by Google as plain text;
* Google Sheets, exported as CSV (Drive exports the first sheet only), parsed into tables;
* PDFs and Word files, downloaded as they are;
* plain-text and Markdown files.

Binary files are handed to the engine as bytes (``FetchedDocument.raw``) and parsed there
by the same parser uploads use: a connector only fetches (CLAUDE.md rule 2). Every other
file is listed too, and counted on the run as skipped with its type (P0-4).

Passes:
* **Incremental** (hourly): files modified after the last pass's high-water mark.
* **Full** (first sync, then weekly): files modified in the last 365 days, at most 500,
  ending with ``keep_only`` so trashed, deleted or no-longer-shared files leave Slate.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

import httpx

from core.adapters.oauth.api import ProviderApi
from core.adapters.oauth.jsonutil import iso, obj, objs, safe_id, strs, text, when
from core.domain.connected import FetchedDocument, Page, cap_sections, clean_title
from core.domain.documents import Section
from core.domain.skips import SkipReason, Skips

API = "https://www.googleapis.com/drive/v3/files"
GOOGLE_DOC = "application/vnd.google-apps.document"
GOOGLE_SHEET = "application/vnd.google-apps.spreadsheet"
GOOGLE_SLIDES = "application/vnd.google-apps.presentation"
PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
TEXT_TYPES = ("text/plain", "text/markdown")
# Google's own formats, exported as: (the export type, whether it's text Slate reads as is).
EXPORTS: dict[str, tuple[str, bool]] = {
    GOOGLE_DOC: ("text/plain", True),
    GOOGLE_SLIDES: ("text/plain", True),
    GOOGLE_SHEET: ("text/csv", False),
}
BINARY_TYPES = (PDF, DOCX)  # downloaded whole and parsed by the engine
NOT_FILES = ("application/vnd.google-apps.folder", "application/vnd.google-apps.shortcut")
_KINDS = {
    "application/pdf": "pdf",
    "application/vnd.google-apps.spreadsheet": "sheet",
    "application/vnd.google-apps.presentation": "slides",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "slides",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "word",
    "application/msword": "word",
}
PER_PULL = 20
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_FULL_FILES = 500  # readable files per full pass
MAX_LISTED = 5000  # files of any type listed per full pass (a Drive of photos ends too)
FULL_EVERY = timedelta(days=7)
FULL_WINDOW = timedelta(days=365)
FIELDS = (
    "nextPageToken,files(id,name,mimeType,modifiedTime,createdTime,webViewLink,size,"
    "owners(displayName,emailAddress))"
)


class DriveConnector:
    def __init__(self, client: httpx.AsyncClient | None = None, *, sleep: float = 1.0) -> None:
        self._api = ProviderApi("Google Drive", frozenset({"www.googleapis.com"}), client,
                                sleep=sleep)  # fmt: skip

    async def pull(self, token: str, checkpoint: Mapping[str, object], *, now: datetime) -> Page:
        cp = dict(checkpoint)
        last_full = when(cp.get("last_full"))
        full = cp.get("mode") == "full" or last_full is None or now - last_full >= FULL_EVERY
        since = (now - FULL_WINDOW) if full else (when(cp.get("since")) or now - FULL_WINDOW)
        # Every file, not only the readable types: what Slate can't read is counted and
        # explained (P0-4) instead of silently missing. Folders and shortcuts aren't files.
        not_files = " and ".join(f"mimeType != '{t}'" for t in NOT_FILES)
        params: dict[str, Any] = {
            "q": f"trashed=false and {not_files} and modifiedTime > '{_rfc3339(since)}'",
            "orderBy": "modifiedTime desc",
            "pageSize": PER_PULL,
            "fields": FIELDS,
        }
        if text(cp.get("page_token")):
            params["pageToken"] = cp["page_token"]
        listing = obj(await self._api.get(token, API, params=params))
        seen = [s for s in strs(cp.get("seen")) if s] if full else []
        newest = when(cp.get("newest"))
        docs: list[FetchedDocument] = []
        skips = Skips()
        files = objs(listing.get("files"))
        listed = _count(cp.get("listed")) + len(files) if full else 0
        for f in files:
            ext = f"gdrive:{f.get('id')}"
            modified = when(f.get("modifiedTime"))
            if modified and (newest is None or modified > newest):
                newest = modified
            skip = unreadable_reason(f)
            if skip is None or skip[0] is not SkipReason.UNSUPPORTED_TYPE:
                seen.append(ext)  # a readable type still exists even if it can't be read now
            if skip is not None:
                skips.add(*skip)
                continue
            body = await self._content(token, f)
            if isinstance(body, SkipReason):
                skips.add(body, _kind_of(str(f.get("mimeType") or "")))
                continue
            doc = drive_document(f, body)
            if doc is None:
                skips.add(SkipReason.EMPTY_TEXT, _kind_of(str(f.get("mimeType") or "")))
            else:
                docs.append(doc)
        next_token = text(listing.get("nextPageToken"))
        capped = bool(next_token) and full and (len(seen) >= MAX_FULL_FILES or listed >= MAX_LISTED)
        if capped:
            skips.add(SkipReason.CAP_REACHED)  # how many more is unknown: flagged once
        if next_token and not capped:
            next_cp = {
                **cp,
                "mode": "full" if full else "incremental",
                "page_token": next_token,
                "newest": iso(newest),
                "seen": seen,
                "listed": listed,
            }
            return Page(tuple(docs), checkpoint=next_cp, done=False, skipped=skips.as_json())
        done_cp: dict[str, object] = {
            "mode": "incremental",
            "since": iso(newest) or cp.get("since"),
            "last_full": now.isoformat() if full else cp.get("last_full"),
        }
        # A full pass that hit the file cap hasn't seen everything: prune nothing then.
        keep = tuple(seen) if full and not next_token else None
        return Page(
            tuple(docs), checkpoint=done_cp, done=True, keep_only=keep, skipped=skips.as_json()
        )

    async def _content(self, token: str, f: Mapping[str, Any]) -> str | bytes | SkipReason:
        """The file's text, or its bytes for the engine to parse, or why it can't be read."""
        file_id = safe_id(str(f.get("id") or ""))
        mime = str(f.get("mimeType") or "")
        if mime in EXPORTS:
            export, is_text = EXPORTS[mime]
            url, params = f"{API}/{file_id}/export", {"mimeType": export}
            if is_text:
                body = await self._api.get_text(
                    token, url, params=params, max_bytes=MAX_FILE_BYTES, allow_404=True
                )
                return SkipReason.UNREADABLE if body is None else body
            return _bounded(await self._api.get_bytes(token, url, params=params, allow_404=True))
        params = {"alt": "media"}
        if mime in TEXT_TYPES:
            body = await self._api.get_text(
                token, f"{API}/{file_id}", params=params, max_bytes=MAX_FILE_BYTES,
                allow_404=True,
            )  # fmt: skip
            return SkipReason.UNREADABLE if body is None else body
        return _bounded(
            await self._api.get_bytes(token, f"{API}/{file_id}", params=params, allow_404=True)
        )


def _bounded(data: bytes | None) -> bytes | SkipReason:
    """A listing's ``size`` is checked first, but exports have none: check the bytes too."""
    if data is None:
        return SkipReason.UNREADABLE
    return SkipReason.TOO_LARGE if len(data) > MAX_FILE_BYTES else data


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _size(f: Mapping[str, Any]) -> int:
    try:
        return int(str(f.get("size") or 0))
    except ValueError:
        return 0


def unreadable_reason(f: Mapping[str, Any]) -> tuple[SkipReason, str] | None:
    """Why a listed file won't be read (pure), or None when Slate reads it."""
    mime = str(f.get("mimeType") or "")
    if mime in EXPORTS:
        return None
    if mime in TEXT_TYPES or mime in BINARY_TYPES:
        return (SkipReason.TOO_LARGE, _kind_of(mime)) if _size(f) > MAX_FILE_BYTES else None
    return SkipReason.UNSUPPORTED_TYPE, _kind_of(mime)


def _kind_of(mime: str) -> str:
    if mime in _KINDS:
        return _KINDS[mime]
    for prefix in ("image/", "video/", "audio/"):
        if mime.startswith(prefix):
            return "video" if prefix == "audio/" else prefix[:-1]
    return "file"


def _rfc3339(moment: datetime) -> str:
    """Drive's query syntax wants whole seconds."""
    return moment.replace(microsecond=0).isoformat()


def raw_mime(drive_mime: str) -> str:
    """The type of the bytes Slate gets for a Drive file (an export or the file itself)."""
    if drive_mime in EXPORTS:
        return EXPORTS[drive_mime][0]
    return drive_mime


def drive_document(f: Mapping[str, Any], body: str | bytes) -> FetchedDocument | None:
    """A Drive file as a document (pure). Text becomes one section; bytes are left for the
    engine's parser (``raw``). None when there is nothing in it."""
    title = clean_title(str(f.get("name") or ""), "Untitled")
    owner = (objs(f.get("owners")) or [{}])[0]
    drive_mime = str(f.get("mimeType") or "")
    common: dict[str, Any] = {
        "external_id": f"gdrive:{f.get('id')}",
        "title": title,
        "uri": text(f.get("webViewLink")) or "https://drive.google.com",
        "author": text(owner.get("displayName")) or text(owner.get("emailAddress")),
        "created_at": when(f.get("createdTime")),
        "updated_at": when(f.get("modifiedTime")),
        "metadata": {"kind": "drive", "mime": drive_mime},
    }
    if isinstance(body, bytes):
        if not body.strip():
            return None
        return FetchedDocument(sections=(), mime=raw_mime(drive_mime), raw=body, **common)
    words = body.replace("\r\n", "\n").replace("\ufeff", "").strip()
    if not words:
        return None
    return FetchedDocument(
        sections=cap_sections([Section(f"Drive > {title}", words)]),
        mime="text/markdown" if drive_mime == "text/markdown" else "text/plain",
        **common,
    )
