"""Google Drive over the Drive API with an OAuth token (ADR-0033). Read-only
(``drive.readonly``; see ADR-0033 for why not ``drive.file``).

Indexed: Google Docs (exported as plain text) and plain-text or Markdown files, not in the
trash, newest-modified first. Every other file is listed too, and counted on the run as
skipped with its type (P0-4), so a Drive of PDFs explains itself. PDFs, Sheets and Slides
come later (P1-1).

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
TEXT_TYPES = ("text/plain", "text/markdown")
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
            if body is None:
                skips.add(SkipReason.UNREADABLE, "file")
                continue
            doc = drive_document(f, body)
            if doc is None:
                skips.add(SkipReason.EMPTY_TEXT, "file")
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

    async def _content(self, token: str, f: Mapping[str, Any]) -> str | None:
        file_id = safe_id(str(f.get("id") or ""))
        mime = str(f.get("mimeType") or "")
        if mime == GOOGLE_DOC:
            return await self._api.get_text(
                token, f"{API}/{file_id}/export", params={"mimeType": "text/plain"},
                max_bytes=MAX_FILE_BYTES, allow_404=True,
            )  # fmt: skip
        if mime in TEXT_TYPES and _size(f) <= MAX_FILE_BYTES:
            return await self._api.get_text(
                token, f"{API}/{file_id}", params={"alt": "media"}, max_bytes=MAX_FILE_BYTES,
                allow_404=True,
            )  # fmt: skip
        return None


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
    if mime == GOOGLE_DOC:
        return None
    if mime in TEXT_TYPES:
        return (SkipReason.TOO_LARGE, "file") if _size(f) > MAX_FILE_BYTES else None
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


def drive_document(f: Mapping[str, Any], body: str) -> FetchedDocument | None:
    """A Drive file's text as a document (pure). None when it has no text."""
    words = body.replace("\r\n", "\n").replace("﻿", "").strip()
    if not words:
        return None
    title = clean_title(str(f.get("name") or ""), "Untitled")
    owner = (objs(f.get("owners")) or [{}])[0]
    return FetchedDocument(
        external_id=f"gdrive:{f.get('id')}",
        title=title,
        uri=text(f.get("webViewLink")) or "https://drive.google.com",
        sections=cap_sections([Section(f"Drive > {title}", words)]),
        mime="text/markdown" if str(f.get("mimeType")) == "text/markdown" else "text/plain",
        author=text(owner.get("displayName")) or text(owner.get("emailAddress")),
        created_at=when(f.get("createdTime")),
        updated_at=when(f.get("modifiedTime")),
        metadata={"kind": "drive", "mime": str(f.get("mimeType") or "")},
    )
