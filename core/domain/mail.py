"""Email rules (ADR-0028). Pure: no I/O.

One **thread** (conversation) is one document: every message in it, oldest first, with quoted
replies stripped so each reply's text appears once. Email is always private data, readable
only by the person who connected the mailbox (a DB CHECK and ADR-0026).

Live mail comes over IMAP from a fixed list of providers (never a user-supplied host: SSRF),
read-only: the mailbox is opened with EXAMINE and messages are fetched with BODY.PEEK, so
nothing is marked read, moved or deleted. Only Inbox and Sent are read, never Spam or Trash.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import cast

from core.domain.attachments import (
    MAX_ATTACHMENTS_LISTED,
    AttachmentBlob,
    AttachmentInfo,
    listing_line,
)
from core.domain.documents import Section

BACKFILL_DAYS = 90
MAX_BACKFILL_THREADS = 2_000
MAX_MESSAGES_PER_FOLDER_PER_SYNC = 5_000  # newest first on the first sync
MAX_THREADS_PER_SYNC = 2_000
MAX_THREAD_MESSAGES = 100
MAX_MESSAGE_CHARS = 20_000
MAX_DOC_CHARS = 100_000
MAX_RAW_MESSAGE_BYTES = 10 * 1024 * 1024  # bigger messages are skipped (attachments)
FETCH_BATCH = 50

_ADDRESS = re.compile(r"^[A-Za-z0-9._%+'-]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,63}$")


class Provider(StrEnum):
    GMAIL = "gmail"
    YAHOO = "yahoo"
    ICLOUD = "icloud"
    FASTMAIL = "fastmail"


@dataclass(frozen=True, slots=True)
class ProviderInfo:
    name: str
    host: str
    port: int
    app_password_url: str
    domains: frozenset[str]  # address domains that suggest this provider


# The only hosts Slate ever connects to (SECURITY.md: no user-supplied hosts). Outlook.com
# is absent on purpose: Microsoft turned off app-password IMAP for consumer accounts.
PROVIDERS: Mapping[Provider, ProviderInfo] = {
    Provider.GMAIL: ProviderInfo(
        "Gmail", "imap.gmail.com", 993, "https://myaccount.google.com/apppasswords",
        frozenset({"gmail.com", "googlemail.com"}),
    ),
    Provider.YAHOO: ProviderInfo(
        "Yahoo Mail", "imap.mail.yahoo.com", 993,
        "https://login.yahoo.com/account/security/app-passwords",
        frozenset({"yahoo.com", "ymail.com", "rocketmail.com"}),
    ),
    Provider.ICLOUD: ProviderInfo(
        "iCloud Mail", "imap.mail.me.com", 993, "https://account.apple.com/account/manage",
        frozenset({"icloud.com", "me.com", "mac.com"}),
    ),
    Provider.FASTMAIL: ProviderInfo(
        "Fastmail", "imap.fastmail.com", 993,
        "https://app.fastmail.com/settings/security/devicekeys",
        frozenset({"fastmail.com", "fastmail.fm"}),
    ),
}  # fmt: skip


def is_valid_address(address: str) -> bool:
    return bool(_ADDRESS.match(address)) and ".." not in address


def normalise_app_password(raw: str) -> str:
    """Providers show app passwords in groups ("abcd efgh ijkl mnop"); the spaces aren't part
    of it. Anything else is kept as typed."""
    return "".join(raw.split())


def is_plausible_app_password(value: str) -> bool:
    """Printable ASCII, 8-200 characters: what providers generate, and what IMAP LOGIN can
    carry (a non-ASCII value can't be sent and would only fail later)."""
    return 8 <= len(value) <= 200 and all(33 <= ord(c) <= 126 for c in value)


class FolderRole(StrEnum):
    INBOX = "inbox"
    SENT = "sent"


@dataclass(frozen=True, slots=True)
class Folder:
    name: str  # the server's name, e.g. "[Gmail]/Sent Mail"
    role: FolderRole


_SENT_NAMES = ("sent", "sent mail", "sent items", "sent messages", "[gmail]/sent mail")


def pick_folders(listing: Sequence[tuple[str, frozenset[str]]]) -> list[Folder]:
    """Inbox and the Sent folder from a LIST reply (name, flags). The Sent folder is found by
    its special-use flag (RFC 6154), else by a well-known name. Spam/Trash never qualify."""
    out = [Folder("INBOX", FolderRole.INBOX)]
    flagged = [n for n, flags in listing if "\\sent" in {f.lower() for f in flags}]
    named = [n for n, _ in listing if n.lower() in _SENT_NAMES]
    sent = (flagged or named or [None])[0]
    if sent is not None:
        out.append(Folder(sent, FolderRole.SENT))
    return out


# ------------------------------------------------------------------ messages


@dataclass(frozen=True, slots=True)
class Mail:
    """One message, already parsed by the adapter (no raw bytes past the edge)."""

    uid: int
    folder: str
    message_id: str
    subject: str
    sender: str  # "Name <addr>" or just the address
    to: tuple[str, ...]
    date: datetime
    body: str
    references: tuple[str, ...] = ()  # Message-IDs from References + In-Reply-To, root first
    gm_thread: str | None = None  # Gmail's X-GM-THRID: an exact conversation id
    attachments: tuple[str, ...] = ()  # display names, for the message text
    cc: tuple[str, ...] = ()
    # Tier 0 for each real attachment (ADR-0043), and its bytes when the adapter was asked
    # to keep them (a sync that indexes attachment text). Never logged (repr=False).
    files: tuple[AttachmentInfo, ...] = ()
    blobs: tuple[AttachmentBlob, ...] = field(default=(), repr=False)
    sent: bool = False  # in the account's Sent folder (Gmail's SENT label)


def thread_key(mail: Mail) -> str:
    """Which conversation a message belongs to. Gmail says so exactly (X-GM-THRID); other
    providers by the first Message-ID in its references, i.e. the thread's root."""
    if mail.gm_thread:
        return f"gm:{mail.gm_thread}"
    root = mail.references[0] if mail.references else mail.message_id
    return f"ref:{root.strip().lower()}"


def thread_external_id(key: str) -> str:
    """A stable, short, opaque id (Message-IDs can be long and contain anything)."""
    return "thread:" + hashlib.sha256(key.encode()).hexdigest()[:32]


_QUOTE_HEADER = re.compile(
    r"^(on .{3,200}wrote:|.{0,80}<[^>]+> (a écrit|schrieb|escribió) ?:|"
    r"-{2,} ?(original message|forwarded message) ?-{2,}|_{10,})\s*$",
    re.IGNORECASE,
)
_HEADER_BLOCK = re.compile(r"^(from|sent|date|to|subject|cc):\s", re.IGNORECASE)


def strip_quoted(text: str) -> str:
    """Remove the quoted history a reply carries ("> ..." lines, "On … wrote:", Outlook's
    header block), so a thread document holds each message's own words once."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if _QUOTE_HEADER.match(stripped):
            break
        if (
            _HEADER_BLOCK.match(stripped)
            and i + 1 < len(lines)
            and _HEADER_BLOCK.match(lines[i + 1].strip())
            and out
            and not out[-1].strip()
        ):
            break  # Outlook-style "From: … / Sent: …" block of a forwarded original
        if stripped.startswith(">"):
            continue
        out.append(line.rstrip())
    while out and not out[-1].strip():
        out.pop()
    text = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def message_text(mail: Mail) -> str:
    head = f"From {mail.sender or 'unknown'} on {mail.date:%Y-%m-%d %H:%M}"
    if mail.to:
        head += " to " + ", ".join(mail.to[:10])
    if mail.cc:
        head += " cc " + ", ".join(mail.cc[:10])
    body = strip_quoted(mail.body)[:MAX_MESSAGE_CHARS] or "(no text)"
    lines = [head + ":", body]
    if mail.files:
        lines += [listing_line(f) for f in mail.files[:20]]
    else:
        lines += [f"[attachment: {n}]" for n in mail.attachments[:20]]
    return "\n".join(lines)


def _subject(mails: Sequence[Mail]) -> str:
    for m in mails:
        s = clean_subject(m.subject)
        if s:
            return s
    return "(no subject)"


_PREFIX = re.compile(r"^\s*((re|fwd?|aw|wg|sv|vs)\s*(\[\d+\])?\s*:\s*)+", re.IGNORECASE)


def clean_subject(subject: str) -> str:
    return _PREFIX.sub("", " ".join(subject.split()))[:300]


def dedupe(mails: Iterable[Mail]) -> list[Mail]:
    """One copy per Message-ID (a message is in Inbox *and* Sent when you mail yourself, and
    a re-read returns it again), oldest first."""
    seen: dict[str, Mail] = {}
    for m in mails:
        key = m.message_id.strip().lower() or f"uid:{m.folder}:{m.uid}"
        first = seen.setdefault(key, m)
        if m.sent and not first.sent:  # the Sent copy of a message also in Inbox
            seen[key] = replace(first, sent=True)
    return sorted(seen.values(), key=lambda m: (m.date, m.message_id))


def thread_sections(mails: Sequence[Mail]) -> tuple[Section, ...]:
    """Built directly (not parsed from Markdown): mail text can't forge document structure."""
    ordered = dedupe(mails)[-MAX_THREAD_MESSAGES:]
    heading = f"Email > {_subject(ordered)}"
    out: list[Section] = []
    used = 0
    for m in ordered:
        text = message_text(m)
        if used + len(text) > MAX_DOC_CHARS:
            room = MAX_DOC_CHARS - used
            if room > 200:
                out.append(Section(heading, text[:room] + "\n(truncated)"))
            break
        out.append(Section(heading, text))
        used += len(text)
    return tuple(out)


def thread_title(mails: Sequence[Mail]) -> str:
    return _subject(dedupe(mails))


def participants(mails: Sequence[Mail], limit: int = 5) -> str | None:
    names: list[str] = []
    for m in dedupe(mails):
        if m.sender and m.sender not in names:
            names.append(m.sender)
    return ", ".join(names[:limit]) or None


def checksum(sections: Sequence[Section], title: str) -> str:
    h = hashlib.sha256(title.encode())
    for s in sections:
        h.update(b"\x00" + (s.heading_path or "").encode() + b"\x00" + s.text.encode())
    return h.hexdigest()


def group_threads(mails: Iterable[Mail]) -> dict[str, list[Mail]]:
    """Messages by conversation, each list oldest first."""
    out: dict[str, list[Mail]] = {}
    for m in mails:
        out.setdefault(thread_key(m), []).append(m)
    return {k: dedupe(v) for k, v in out.items()}


def newest_threads(threads: Mapping[str, Sequence[Mail]], limit: int) -> list[str]:
    """Thread keys, most recent activity first, capped (the backfill keeps the newest)."""
    return sorted(threads, key=lambda k: max(m.date for m in threads[k]), reverse=True)[:limit]


# ------------------------------------------------------------------ checkpoint


@dataclass(frozen=True, slots=True)
class FolderCursor:
    """IMAP UIDs only grow within one UIDVALIDITY. If the server changes UIDVALIDITY (it
    rebuilt the folder), every saved UID is meaningless and the folder is re-read from the
    backfill date; unchanged threads then cost nothing (checksum)."""

    uidvalidity: int | None = None
    last_uid: int = 0

    def valid_for(self, uidvalidity: int) -> bool:
        return self.uidvalidity == uidvalidity and self.last_uid > 0


MAIL_CHECKPOINT_VERSION = 2  # 2: attachments are read (ADR-0043)


@dataclass(frozen=True, slots=True)
class MailCheckpoint:
    folders: Mapping[str, FolderCursor] = field(default_factory=dict[str, FolderCursor])

    def cursor(self, folder: str) -> FolderCursor:
        return self.folders.get(folder, FolderCursor())

    def with_cursor(self, folder: str, cursor: FolderCursor) -> MailCheckpoint:
        return replace(self, folders={**self.folders, folder: cursor})

    @property
    def first_sync(self) -> bool:
        return not any(c.last_uid for c in self.folders.values())

    def as_json(self) -> dict[str, object]:
        return {
            "v": MAIL_CHECKPOINT_VERSION,
            "folders": {
                name: {"uidvalidity": c.uidvalidity, "last_uid": c.last_uid}
                for name, c in self.folders.items()
            },
        }

    @classmethod
    def from_json(cls, raw: object) -> MailCheckpoint:
        folders: dict[str, FolderCursor] = {}
        if _obj(raw) and _obj(raw).get("v") != MAIL_CHECKPOINT_VERSION:
            return cls()  # written before attachments were read (ADR-0043): re-read once
        for name, c in _obj(_obj(raw).get("folders")).items():
            data = _obj(c)
            uv, last = data.get("uidvalidity"), data.get("last_uid")
            if len(name) <= 200 and isinstance(last, int) and last >= 0:
                folders[name] = FolderCursor(
                    uidvalidity=uv if isinstance(uv, int) else None, last_uid=last
                )
        return cls(folders=folders)


def _obj(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict):
        return {}
    return {str(k): v for k, v in cast(dict[object, object], raw).items()}


def backfill_since(now: datetime) -> datetime:
    return now - timedelta(days=BACKFILL_DAYS)


MAX_PEOPLE = 30


def _address_only(person: str) -> str:
    m = re.search(r"<([^<>]+)>\s*$", person)
    return (m.group(1) if m else person).strip().lower()


def thread_metadata(mails: Sequence[Mail]) -> dict[str, object]:
    """What search filters and answers need about a conversation (ADR-0043): who was in
    it, whether the account sent or received it, and its attachments (Tier 0)."""
    ordered = sorted(dedupe(mails), key=lambda m: m.date)
    people: list[str] = []
    recipients: list[str] = []
    for m in ordered:
        for p in (m.sender, *m.to, *m.cc):
            if p and _address_only(p) not in {_address_only(x) for x in people}:
                people.append(p)
        for p in (*m.to, *m.cc):
            if p and p not in recipients:
                recipients.append(p)
    folders = sorted({"sent" if m.sent else "inbox" for m in ordered})
    files = [f for m in ordered for f in m.files][:MAX_ATTACHMENTS_LISTED]
    meta: dict[str, object] = {
        "kind": "email",
        "messages": len(ordered),
        "folders": folders,
        "people": [p[:200] for p in people[:MAX_PEOPLE]],
        "recipients": ", ".join(recipients[:10])[:400],
    }
    if files:
        meta["attachments"] = [f.to_meta() for f in files]
    return meta


MAX_ATTACHMENT_BYTES_PER_RUN = 60 * 1024 * 1024


def cap_attachment_bytes(mails: Sequence[Mail]) -> list[Mail]:
    """Drop kept attachment bytes past the run's memory budget (newest mail first kept).
    Dropped files stay listed (Tier 0) and are read on demand (ADR-0043)."""
    used = 0
    out: list[Mail] = []
    for m in sorted(mails, key=lambda m: m.date, reverse=True):
        kept: list[AttachmentBlob] = []
        for b in m.blobs:
            if used + len(b.data) <= MAX_ATTACHMENT_BYTES_PER_RUN:
                used += len(b.data)
                kept.append(b)
        out.append(replace(m, blobs=tuple(kept)) if len(kept) != len(m.blobs) else m)
    return out


def thread_files(
    mails: Sequence[Mail],
) -> tuple[tuple[AttachmentInfo, ...], tuple[AttachmentBlob, ...]]:
    """Tier 0 for every attachment in the thread, and the bytes the adapter kept."""
    ordered = sorted(dedupe(mails), key=lambda m: m.date)
    files = tuple(f for m in ordered for f in m.files)[:MAX_ATTACHMENTS_LISTED]
    keys = {f.key for f in files}
    blobs = tuple(b for m in ordered for b in m.blobs if b.info.key in keys)
    return files, blobs
