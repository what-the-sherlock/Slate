"""Gmail over the Gmail API with an OAuth token (ADR-0033). Read-only (``gmail.readonly``).

Same documents as the IMAP connector (ADR-0028): one per conversation, built by the shared
email rules (``core.domain.mail``) from the raw RFC 822 messages, so quoted replies are
stripped and attachments are listed by name but never read.

Passes:
* **Backfill:** threads in Inbox or Sent from the last 90 days, newest first, at most 2,000.
  Gmail's history id is taken *before* listing, so nothing that changes during the backfill
  is missed afterwards.
* **Incremental:** ``history.list`` from the saved history id; every conversation it touches
  is re-read whole (a new reply, a deletion, a label change). A conversation that no longer
  exists, or holds only spam, trash or drafts, is deleted from Slate.
* **History expired** (Gmail keeps about a week; 404): back to a backfill, where unchanged
  conversations cost nothing (checksums).

Touched conversation ids wait in the checkpoint (``pending``), so a stopped run resumes
exactly where it was.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import httpx

from core.adapters.mail.parse import parse_mail
from core.adapters.oauth.api import ProviderApi
from core.adapters.oauth.jsonutil import obj, objs, strs, text
from core.domain.connected import FetchedDocument, Page
from core.domain.mail import (
    MAX_BACKFILL_THREADS,
    MAX_RAW_MESSAGE_BYTES,
    MAX_THREAD_MESSAGES,
    Mail,
    participants,
    thread_external_id,
    thread_sections,
    thread_title,
)

BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
QUERY = "(in:inbox OR in:sent) newer_than:90d"
THREADS_PER_PAGE = 20
MAX_PENDING = 5_000
_SKIP_LABELS = frozenset({"SPAM", "TRASH", "DRAFT"})


class GmailConnector:
    def __init__(self, client: httpx.AsyncClient | None = None, *, sleep: float = 1.0) -> None:
        self._api = ProviderApi("Gmail", frozenset({"gmail.googleapis.com"}), client, sleep=sleep)

    async def pull(self, token: str, checkpoint: Mapping[str, object], *, now: datetime) -> Page:
        cp = _Checkpoint.load(checkpoint)
        if cp.phase == "incremental":
            return await self._incremental(token, cp)
        return await self._backfill(token, cp)

    async def _backfill(self, token: str, cp: _Checkpoint) -> Page:
        if cp.history_id is None:
            profile = await self._api.get(token, f"{BASE}/profile")
            cp.history_id = str(obj(profile).get("historyId") or "") or None
        if not cp.pending and (cp.count == 0 or cp.page_token):
            params: dict[str, str | int] = {"q": QUERY, "maxResults": 50}
            if cp.page_token:
                params["pageToken"] = cp.page_token
            listing = obj(await self._api.get(token, f"{BASE}/threads", params=params))
            cp.pending = [str(t.get("id")) for t in objs(listing.get("threads")) if t.get("id")]
            cp.page_token = text(listing.get("nextPageToken"))
        page = await self._threads(token, cp)
        cp.count += len(page.documents) + len(page.deleted)
        if not cp.pending and (not cp.page_token or cp.count >= MAX_BACKFILL_THREADS):
            cp.phase, cp.page_token, cp.count = "incremental", None, 0
            return Page(page.documents, page.deleted, cp.dump(), done=True)
        return Page(page.documents, page.deleted, cp.dump(), done=False)

    async def _incremental(self, token: str, cp: _Checkpoint) -> Page:
        if not cp.pending:
            params: dict[str, Any] = {
                "startHistoryId": cp.history_id,
                "historyTypes": ["messageAdded", "messageDeleted", "labelAdded", "labelRemoved"],
                "maxResults": 500,
            }
            if cp.page_token:
                params["pageToken"] = cp.page_token
            history = await self._api.get(token, f"{BASE}/history", params=params, allow_404=True)
            if history is None or cp.history_id is None:  # history expired: rescan
                fresh = _Checkpoint()
                return Page(checkpoint=fresh.dump(), done=False)
            body = obj(history)
            touched: dict[str, None] = {}
            for entry in objs(body.get("history")):
                for m in objs(entry.get("messages")):
                    if m.get("threadId"):
                        touched[str(m["threadId"])] = None
            cp.pending = list(touched)[:MAX_PENDING]
            cp.page_token = text(body.get("nextPageToken"))
            if cp.page_token is None and body.get("historyId"):
                cp.history_id = str(body["historyId"])
        page = await self._threads(token, cp)
        done = not cp.pending and cp.page_token is None
        return Page(page.documents, page.deleted, cp.dump(), done=done)

    async def _threads(self, token: str, cp: _Checkpoint) -> Page:
        batch, cp.pending = cp.pending[:THREADS_PER_PAGE], cp.pending[THREADS_PER_PAGE:]
        docs: list[FetchedDocument] = []
        deleted: list[str] = []
        for thread_id in batch:
            doc = await self._thread(token, thread_id)
            if doc is None:
                deleted.append(thread_external_id(f"gm:{thread_id}"))
            else:
                docs.append(doc)
        return Page(tuple(docs), tuple(deleted))

    async def _thread(self, token: str, thread_id: str) -> FetchedDocument | None:
        thread = await self._api.get(
            token, f"{BASE}/threads/{_safe(thread_id)}", params={"format": "minimal"},
            allow_404=True,
        )  # fmt: skip
        if thread is None:
            return None
        refs = [
            m for m in objs(obj(thread).get("messages"))
            if not _SKIP_LABELS & set(strs(m.get("labelIds")))
            and int(m.get("sizeEstimate") or 0) <= MAX_RAW_MESSAGE_BYTES
        ][-MAX_THREAD_MESSAGES:]  # fmt: skip
        mails: list[Mail] = []
        for i, ref in enumerate(refs):
            raw = await self._api.get(
                token, f"{BASE}/messages/{_safe(str(ref.get('id')))}", params={"format": "raw"},
                allow_404=True,
            )  # fmt: skip
            mail = _parse(raw, i, thread_id, ref.get("internalDate"))
            if mail is not None:
                mails.append(mail)
        return gmail_document(thread_id, mails)


def gmail_document(thread_id: str, mails: Sequence[Mail]) -> FetchedDocument | None:
    """A conversation as a document (the IMAP connector's rules), or None when it has no
    readable message left."""
    sections = thread_sections(mails)
    if not sections:
        return None
    ordered = sorted(mails, key=lambda m: m.date)
    return FetchedDocument(
        external_id=thread_external_id(f"gm:{thread_id}"),
        title=thread_title(mails),
        uri=f"https://mail.google.com/mail/u/0/#all/{_safe(thread_id)}",
        sections=sections,
        mime="message/rfc822",
        author=participants(ordered),
        created_at=ordered[0].date,
        updated_at=ordered[-1].date,
        metadata={"kind": "email", "messages": len(ordered)},
    )


def _parse(raw: object, uid: int, thread_id: str, internal_ms: object) -> Mail | None:
    data = obj(raw).get("raw")
    if not isinstance(data, str):
        return None
    try:
        decoded = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except ValueError:
        return None
    if len(decoded) > MAX_RAW_MESSAGE_BYTES:
        return None
    when = None
    try:
        when = datetime.fromtimestamp(int(str(internal_ms)) / 1000, tz=UTC)
    except (TypeError, ValueError, OverflowError):
        when = None
    return parse_mail(decoded, uid=uid, folder="gmail", gm_thread=thread_id, internal_date=when)


class _Checkpoint:
    def __init__(self) -> None:
        self.phase = "backfill"
        self.history_id: str | None = None
        self.page_token: str | None = None
        self.pending: list[str] = []
        self.count = 0

    @classmethod
    def load(cls, raw: Mapping[str, object]) -> _Checkpoint:
        cp = cls()
        cp.phase = "incremental" if raw.get("phase") == "incremental" else "backfill"
        cp.history_id = text(raw.get("history_id"))
        cp.page_token = text(raw.get("page_token"))
        cp.pending = [p for p in strs(raw.get("pending")) if p][:MAX_PENDING]
        count = raw.get("count")
        cp.count = count if isinstance(count, int) and count >= 0 else 0
        if cp.phase == "incremental" and cp.history_id is None:
            cp.phase = "backfill"
        return cp

    def dump(self) -> dict[str, object]:
        return {
            "phase": self.phase,
            "history_id": self.history_id,
            "page_token": self.page_token,
            "pending": self.pending,
            "count": self.count,
        }


def _safe(value: str) -> str:
    """Gmail ids are hex; anything else never reaches a URL path."""
    return value if value.isalnum() and len(value) <= 64 else "invalid"
