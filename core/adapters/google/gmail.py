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

Attachments (ADR-0043): a message's raw bytes already hold its files, so readable ones are
handed to the engine (``FetchedDocument.attachments``) with no extra request. A message
over 10 MB is read with ``format=full`` instead of being skipped: its text is indexed and
its files listed, each fetched on its own when someone asks (``fetch_attachment``). A
checkpoint written before attachments were read (``v`` < 2) starts one backfill, where
unchanged conversations cost nothing (checksums).
"""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any

import httpx

from core.adapters.mail.parse import attachment_part, html_to_text, parse_mail
from core.adapters.oauth.api import ProviderApi
from core.adapters.oauth.jsonutil import obj, objs, strs, text
from core.domain.attachments import (
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENTS_LISTED,
    AttachmentBlob,
    AttachmentInfo,
    clean_name,
    key_seed,
)
from core.domain.connected import FetchedDocument, Page
from core.domain.mail import (
    MAX_BACKFILL_THREADS,
    MAX_MESSAGE_CHARS,
    MAX_RAW_MESSAGE_BYTES,
    MAX_THREAD_MESSAGES,
    Mail,
    participants,
    thread_external_id,
    thread_files,
    thread_metadata,
    thread_sections,
    thread_title,
)

BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
QUERY = "(in:inbox OR in:sent) newer_than:90d"
THREADS_PER_PAGE = 20
MAX_PENDING = 5_000
_SKIP_LABELS = frozenset({"SPAM", "TRASH", "DRAFT"})
CHECKPOINT_VERSION = 2  # 2: attachments are read (ADR-0043); older ones re-backfill once


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
        ][-MAX_THREAD_MESSAGES:]  # fmt: skip
        mails: list[Mail] = []
        for i, ref in enumerate(refs):
            msg_id = _safe(str(ref.get("id")))
            big = int(ref.get("sizeEstimate") or 0) > MAX_RAW_MESSAGE_BYTES
            raw = await self._api.get(
                token, f"{BASE}/messages/{msg_id}", params={"format": "full" if big else "raw"},
                allow_404=True,
            )  # fmt: skip
            when = ref.get("internalDate")
            mail = _parse_full(raw, i, thread_id, when) if big else _parse(raw, i, thread_id, when)
            if mail is not None:
                sent = "SENT" in set(strs(ref.get("labelIds")))
                mails.append(_with_message(mail, msg_id, sent=sent))
        return gmail_document(thread_id, mails)

    async def fetch_attachment(self, token: str, ref: Mapping[str, str]) -> bytes | None:
        """One attachment's bytes, fetched again through its message (ADR-0043); None
        when the message or the file is gone. Gmail's attachment ids aren't stable, so a
        large message's file is found again by its part id."""
        msg_id = _safe(ref.get("message", ""))
        if msg_id == "invalid":
            return None
        if "gpart" in ref:
            full = await self._api.get(
                token, f"{BASE}/messages/{msg_id}", params={"format": "full"}, allow_404=True
            )
            part = _find_part(obj(full).get("payload"), ref["gpart"]) if full else None
            body = obj(part.get("body")) if part else {}
            att = text(body.get("attachmentId"))
            if not att or int(body.get("size") or 0) > MAX_ATTACHMENT_BYTES:
                return None
            got = await self._api.get(
                token, f"{BASE}/messages/{msg_id}/attachments/{_safe_att(att)}", allow_404=True
            )
            return _b64(obj(got).get("data")) if got else None
        raw = await self._api.get(
            token, f"{BASE}/messages/{msg_id}", params={"format": "raw"}, allow_404=True
        )
        data = _b64(obj(raw).get("raw")) if raw else None
        if data is None or len(data) > MAX_RAW_MESSAGE_BYTES:
            return None
        try:
            part_index = int(ref.get("part", ""))
        except ValueError:
            return None
        return attachment_part(data, part_index)


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
        metadata=thread_metadata(mails),
        attachments=thread_files(mails)[1],
    )


def _with_message(mail: Mail, msg_id: str, *, sent: bool) -> Mail:
    """Point each attachment's ref at its Gmail message, so it can be fetched again."""

    def ref(info: AttachmentInfo) -> AttachmentInfo:
        return replace(info, ref={**info.ref, "message": msg_id})

    files = tuple(ref(f) for f in mail.files)
    blobs = tuple(AttachmentBlob(ref(b.info), b.data) for b in mail.blobs)
    return replace(mail, files=files, blobs=blobs, sent=sent)


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
    return parse_mail(
        decoded, uid=uid, folder="gmail", gm_thread=thread_id,
        internal_date=_internal(internal_ms), keep_attachments=True,
    )  # fmt: skip


def _internal(internal_ms: object) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(str(internal_ms)) / 1000, tz=UTC)
    except (TypeError, ValueError, OverflowError):
        return None


def _b64(data: object) -> bytes | None:
    if not isinstance(data, str):
        return None
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except ValueError:
        return None


def _headers(payload: Mapping[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for h in objs(payload.get("headers")):
        name, value = text(h.get("name")), text(h.get("value"))
        if name and value is not None:
            out.setdefault(name.lower(), value)
    return out


def _walk(payload: object) -> list[Mapping[str, Any]]:
    """Leaf parts of a ``format=full`` payload, depth-first (at most 500)."""
    out: list[Mapping[str, Any]] = []
    stack = [obj(payload)]
    while stack and len(out) < 500:
        part = stack.pop()
        children = objs(part.get("parts"))
        if children:
            stack.extend(reversed(children))
        else:
            out.append(part)
    return out


def _find_part(payload: object, part_id: str) -> Mapping[str, Any] | None:
    return next((p for p in _walk(payload) if text(p.get("partId")) == part_id), None)


def _people(value: str | None) -> tuple[str, ...]:
    found = getaddresses([value or ""])[:20]
    return tuple(f"{n} <{a}>" if n and a else (a or n) for n, a in found if a or n)


def _parse_full(raw: object, uid: int, thread_id: str, internal_ms: object) -> Mail | None:
    """A message too big for ``format=raw``: headers and text from Gmail's parsed form,
    and its files listed (Tier 0) with a ref to fetch each one alone."""
    payload = obj(obj(raw).get("payload"))
    if not payload:
        return None
    h = _headers(payload)
    message_id = h.get("message-id", "").strip() or f"<gm-{thread_id}-{uid}@slate.invalid>"
    seed = key_seed(message_id)
    plain: str | None = None
    html: str | None = None
    files: list[AttachmentInfo] = []
    for j, part in enumerate(_walk(payload)):
        mime = (text(part.get("mimeType")) or "application/octet-stream").lower()
        name = text(part.get("filename"))
        body = obj(part.get("body"))
        if name:
            if len(files) < MAX_ATTACHMENTS_LISTED:
                files.append(
                    AttachmentInfo(
                        key=f"{seed}-g{j}", name=clean_name(name), mime=mime,
                        size=int(body.get("size") or 0),
                        ref={"gpart": text(part.get("partId")) or str(j)},
                    )
                )  # fmt: skip
            continue
        data = _b64(body.get("data"))
        if data is None:
            continue
        if mime == "text/plain" and plain is None:
            plain = data.decode("utf-8", "replace")
        elif mime == "text/html" and html is None:
            html = data.decode("utf-8", "replace")
    try:
        date = parsedate_to_datetime(h["date"]) if h.get("date") else None
    except (TypeError, ValueError, IndexError):
        date = None
    date = date or _internal(internal_ms) or datetime.now(UTC)
    body_text = plain if plain and plain.strip() else (html_to_text(html) if html else "")
    sender = _people(h.get("from"))
    return Mail(
        uid=uid,
        folder="gmail",
        message_id=message_id,
        subject=h.get("subject", "")[:500],
        sender=sender[0] if sender else "",
        to=_people(h.get("to")),
        cc=_people(h.get("cc")),
        date=date if date.tzinfo else date.replace(tzinfo=UTC),
        body=body_text[: MAX_MESSAGE_CHARS * 2],
        gm_thread=thread_id,
        attachments=tuple(f.name for f in files),
        files=tuple(files),
    )


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
        if raw and raw.get("v") != CHECKPOINT_VERSION:
            return cls()  # written before attachments were read: one fresh backfill
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
            "v": CHECKPOINT_VERSION,
            "phase": self.phase,
            "history_id": self.history_id,
            "page_token": self.page_token,
            "pending": self.pending,
            "count": self.count,
        }


def _safe_att(value: str) -> str:
    """Attachment ids are long base64url tokens; anything else never reaches a URL."""
    ok = 0 < len(value) <= 2048 and all(c.isalnum() or c in "-_" for c in value)
    return value if ok else "invalid"


def _safe(value: str) -> str:
    """Gmail ids are hex; anything else never reaches a URL path."""
    return value if value.isalnum() and len(value) <= 64 else "invalid"
