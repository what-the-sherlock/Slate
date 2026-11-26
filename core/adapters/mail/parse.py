"""RFC 5322 bytes -> ``Mail`` (ADR-0028). Stdlib ``email`` with the modern policy, which
decodes encoded headers (=?utf-8?...?=) and charsets. Untrusted input: never raises, so one
broken message can't stop a sync; it becomes None and is skipped."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime

from bs4 import BeautifulSoup

from core.domain.mail import MAX_MESSAGE_CHARS, Mail

_MSGID = re.compile(r"<[^<>\s]{1,500}>")
_MAX_HTML_CHARS = 2_000_000


def _addresses(raw: str | None, limit: int = 20) -> tuple[str, ...]:
    out: list[str] = []
    for name, addr in getaddresses([raw or ""])[:limit]:
        if not addr and not name:
            continue
        out.append(f"{name} <{addr}>" if name and addr else (addr or name))
    return tuple(out)


def _header(msg: EmailMessage, name: str) -> str:
    try:
        value = msg.get(name)
        return str(value) if value is not None else ""
    except (ValueError, TypeError, IndexError):  # malformed encoded words
        return ""


def _date(msg: EmailMessage, fallback: datetime | None) -> datetime:
    raw = _header(msg, "Date")
    try:
        when = parsedate_to_datetime(raw) if raw else None
    except (TypeError, ValueError, IndexError):
        when = None
    when = when or fallback or datetime.now(UTC)
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html[:_MAX_HTML_CHARS], "html.parser")
    for tag in soup(["script", "style", "head", "title"]):
        tag.decompose()
    for quote in soup.select("blockquote, .gmail_quote, #divRplyFwdMsg, #appendonsend"):
        quote.decompose()  # quoted history: the thread already has those messages
    text = soup.get_text("\n")
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _body(msg: EmailMessage) -> tuple[str, tuple[str, ...]]:
    attachments: list[str] = []
    plain: str | None = None
    html: str | None = None
    for part in msg.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        if filename or part.get_content_disposition() == "attachment":
            attachments.append(str(filename or "attachment")[:200])
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            content = part.get_content()
        except (LookupError, ValueError, AssertionError):  # unknown charset, bad encoding
            payload = part.get_payload(decode=True)
            content = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else ""
        if not isinstance(content, str):
            continue
        if ctype == "text/plain" and plain is None:
            plain = content
        elif ctype == "text/html" and html is None:
            html = content
    text = plain if plain and plain.strip() else (html_to_text(html) if html else "")
    return text[: MAX_MESSAGE_CHARS * 2], tuple(attachments[:50])


def parse_mail(
    raw: bytes,
    *,
    uid: int,
    folder: str,
    gm_thread: str | None = None,
    internal_date: datetime | None = None,
) -> Mail | None:
    try:
        msg = BytesParser(policy=policy.default).parsebytes(raw)
        refs = _MSGID.findall(_header(msg, "References"))
        reply = _MSGID.findall(_header(msg, "In-Reply-To"))
        references = tuple(dict.fromkeys([*refs, *reply]))[:50]
        message_id = (_MSGID.findall(_header(msg, "Message-ID")) or [""])[0]
        body, attachments = _body(msg)
        sender = _addresses(_header(msg, "From"), 1)
        return Mail(
            uid=uid,
            folder=folder,
            message_id=message_id or f"<uid-{folder}-{uid}@slate.invalid>",
            subject=_header(msg, "Subject")[:500],
            sender=sender[0] if sender else "",
            to=_addresses(_header(msg, "To")),
            cc=_addresses(_header(msg, "Cc")),
            date=_date(msg, internal_date),
            body=body,
            references=references,
            gm_thread=gm_thread,
            attachments=attachments,
        )
    except Exception:  # untrusted bytes: skip the message, never the sync
        return None


_FROM_LINE = re.compile(rb"^From [^\r\n]*\r?\n", re.MULTILINE)
_ESCAPED_FROM = re.compile(rb"^>(>*From )", re.MULTILINE)
_GM_THRID = re.compile(rb"^X-GM-THRID:\s*(\d{1,25})\s*$", re.MULTILINE | re.IGNORECASE)


def parse_mbox(data: bytes) -> list[Mail]:
    """Split an mbox (Google Takeout writes mboxrd) into messages. Takeout keeps Gmail's
    conversation id in an ``X-GM-THRID`` header, so threads match the live Gmail sync."""
    starts = [m.start() for m in _FROM_LINE.finditer(data)]
    out: list[Mail] = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(data)
        block = data[start:end]
        raw = _ESCAPED_FROM.sub(rb"\1", block[block.find(b"\n") + 1 :])
        header_end = raw.find(b"\n\n")
        thrid = _GM_THRID.search(raw[: header_end if header_end > 0 else 65536])
        mail = parse_mail(
            raw, uid=i + 1, folder="mbox", gm_thread=thrid.group(1).decode() if thrid else None
        )
        if mail is not None:
            out.append(mail)
    return out
