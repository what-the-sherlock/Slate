"""Google Drive over the Drive API with an OAuth token (ADR-0033). Read-only
(``drive.readonly``; see ADR-0033 for why not ``drive.file``).

Indexed: Google Docs (exported as plain text) and plain-text or Markdown files, not in the
trash, newest-modified first. PDFs, Sheets and Slides come later.

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

API = "https://www.googleapis.com/drive/v3/files"
GOOGLE_DOC = "application/vnd.google-apps.document"
TEXT_TYPES = ("text/plain", "text/markdown")
PER_PULL = 20
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_FULL_FILES = 500
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
        types = " or ".join(f"mimeType='{t}'" for t in (GOOGLE_DOC, *TEXT_TYPES))
        params: dict[str, Any] = {
            "q": f"trashed=false and ({types}) and modifiedTime > '{_rfc3339(since)}'",
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
        for f in objs(listing.get("files")):
            ext = f"gdrive:{f.get('id')}"
            seen.append(ext)  # still exists even if it can't be read now
            modified = when(f.get("modifiedTime"))
            if modified and (newest is None or modified > newest):
                newest = modified
            body = await self._content(token, f)
            doc = drive_document(f, body) if body is not None else None
            if doc is not None:
                docs.append(doc)
        next_token = text(listing.get("nextPageToken"))
        if next_token and (not full or len(seen) < MAX_FULL_FILES):
            next_cp = {
                **cp,
                "mode": "full" if full else "incremental",
                "page_token": next_token,
                "newest": iso(newest),
                "seen": seen,
            }
            return Page(tuple(docs), checkpoint=next_cp, done=False)
        done_cp: dict[str, object] = {
            "mode": "incremental",
            "since": iso(newest) or cp.get("since"),
            "last_full": now.isoformat() if full else cp.get("last_full"),
        }
        # A full pass that hit the file cap hasn't seen everything: prune nothing then.
        keep = tuple(seen) if full and not next_token else None
        return Page(tuple(docs), checkpoint=done_cp, done=True, keep_only=keep)

    async def _content(self, token: str, f: Mapping[str, Any]) -> str | None:
        file_id = safe_id(str(f.get("id") or ""))
        mime = str(f.get("mimeType") or "")
        if mime == GOOGLE_DOC:
            return await self._api.get_text(
                token, f"{API}/{file_id}/export", params={"mimeType": "text/plain"},
                max_bytes=MAX_FILE_BYTES, allow_404=True,
            )  # fmt: skip
        try:
            size = int(str(f.get("size") or 0))
        except ValueError:
            size = 0
        if mime in TEXT_TYPES and size <= MAX_FILE_BYTES:
            return await self._api.get_text(
                token, f"{API}/{file_id}", params={"alt": "media"}, max_bytes=MAX_FILE_BYTES,
                allow_404=True,
            )  # fmt: skip
        return None


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
