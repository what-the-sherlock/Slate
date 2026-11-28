"""RFC 5322 bytes -> ``Mail`` (ADR-0028). Stdlib ``email`` with the modern policy, which
decodes encoded headers (=?utf-8?...?=) and charsets. Untrusted input: never raises, so one
broken message can't stop a sync; it becomes None and is skipped."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from typing import cast

from bs4 import BeautifulSoup

from core.domain.attachments import (
    MAX_ATTACHMENTS_LISTED,
    MAX_BYTES_PER_PAGE,
    AttachmentBlob,
    AttachmentInfo,
    clean_name,
    first_pass_state,
)
from core.domain.attachments import key_seed as seed_of
from core.domain.mail import MAX_MESSAGE_CHARS, Mail, message_text

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


def _parts(part: EmailMessage, *, root: bool = True) -> Iterator[EmailMessage]:
    """Leaf parts, plus attached messages as single parts: unlike ``walk()``, this never
    descends into a forwarded message/rfc822, so its body and files stay its own."""
    if not root and part.get_content_type() == "message/rfc822":
        yield part
        return
    if part.is_multipart():
        for sub in part.iter_parts():
            if isinstance(sub, EmailMessage):
                yield from _parts(sub, root=False)
        return
    yield part


def _is_inline_image(part: EmailMessage) -> bool:
    """Signature logos and pasted pictures: shown in the body, not sent as files."""
    return (
        part.get_content_maintype() == "image"
        and part.get_content_disposition() != "attachment"
        and (part.get_content_disposition() == "inline" or part.get("Content-ID") is not None)
    )


def _attached_message(part: EmailMessage) -> tuple[str, bytes]:
    """A forwarded .eml as (name, text), read one level deep (its own files are only
    listed)."""
    inner: object = part.get_payload()
    first = cast(list[object], inner)[0] if isinstance(inner, list) and inner else None
    raw = first.as_bytes() if isinstance(first, EmailMessage) else b""
    nested = parse_mail(raw, uid=0, folder="attached") if raw else None
    if nested is None:
        return "forwarded message.eml", b""
    name = clean_name(f"{nested.subject or 'forwarded message'}.eml")
    return name, message_text(nested).encode()


def _body(msg: EmailMessage, *, key_seed: str, keep_bytes: bool) -> _Body:
    out = _Body()
    plain: str | None = None
    html: str | None = None
    seed = seed_of(key_seed)
    for j, part in enumerate(_parts(msg)):
        ctype = part.get_content_type()
        try:
            filename = part.get_filename()
        except (ValueError, TypeError, IndexError):  # malformed RFC 2231 parameters
            filename = None
        is_file = ctype == "message/rfc822" or (
            (filename is not None or part.get_content_disposition() == "attachment")
            and not _is_inline_image(part)
        )
        if is_file:
            if len(out.files) < MAX_ATTACHMENTS_LISTED:
                out.add_file(part, j, seed, filename, keep_bytes=keep_bytes)
            continue
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
    out.text = text[: MAX_MESSAGE_CHARS * 2]
    return out


def _where(info: AttachmentInfo, folder: str, uid: int) -> AttachmentInfo:
    """Where to read the file again (IMAP: folder + UID; Gmail adds its message id)."""
    return replace(info, ref={**info.ref, "folder": folder[:200], "uid": str(uid)})


class _Body:
    def __init__(self) -> None:
        self.text = ""
        self.files: list[AttachmentInfo] = []
        self.blobs: list[AttachmentBlob] = []
        self._kept = 0

    def add_file(
        self, part: EmailMessage, j: int, seed: str, filename: str | None, *, keep_bytes: bool
    ) -> None:
        ctype = part.get_content_type()
        if ctype == "message/rfc822":
            name, data = _attached_message(part)
        else:
            name = clean_name(filename)
            payload = part.get_payload(decode=True)
            data = payload if isinstance(payload, bytes) else b""
        info = AttachmentInfo(
            key=f"{seed}-{j}", name=name, mime=ctype, size=len(data), ref={"part": str(j)}
        )
        position = len(self.files)
        self.files.append(info)
        if (
            keep_bytes
            and data
            and first_pass_state(info, position=position) is None
            and self._kept + len(data) <= MAX_BYTES_PER_PAGE
        ):
            self._kept += len(data)
            self.blobs.append(AttachmentBlob(info, data))


def parse_mail(
    raw: bytes,
    *,
    uid: int,
    folder: str,
    gm_thread: str | None = None,
    internal_date: datetime | None = None,
    keep_attachments: bool = False,
) -> Mail | None:
    """``keep_attachments`` keeps readable attachments' bytes on ``Mail.blobs`` (the
    engine sniffs and parses them; ADR-0043). Names and sizes are always listed."""
    try:
        msg = BytesParser(policy=policy.default).parsebytes(raw)
        refs = _MSGID.findall(_header(msg, "References"))
        reply = _MSGID.findall(_header(msg, "In-Reply-To"))
        references = tuple(dict.fromkeys([*refs, *reply]))[:50]
        message_id = (_MSGID.findall(_header(msg, "Message-ID")) or [""])[0]
        body = _body(msg, key_seed=message_id or f"{folder}:{uid}", keep_bytes=keep_attachments)
        sender = _addresses(_header(msg, "From"), 1)
        # Google Takeout labels each message; "Sent" is the account's own mail.
        labels = {x.strip().lower() for x in _header(msg, "X-Gmail-Labels").split(",")}
        return Mail(
            uid=uid,
            folder=folder,
            message_id=message_id or f"<uid-{folder}-{uid}@slate.invalid>",
            subject=_header(msg, "Subject")[:500],
            sender=sender[0] if sender else "",
            to=_addresses(_header(msg, "To")),
            cc=_addresses(_header(msg, "Cc")),
            date=_date(msg, internal_date),
            body=body.text,
            references=references,
            gm_thread=gm_thread,
            attachments=tuple(f.name for f in body.files),
            files=tuple(_where(f, folder, uid) for f in body.files),
            blobs=tuple(AttachmentBlob(_where(b.info, folder, uid), b.data) for b in body.blobs),
            sent="sent" in labels,
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
            raw, uid=i + 1, folder="mbox", gm_thread=thrid.group(1).decode() if thrid else None,
            keep_attachments=True,
        )  # fmt: skip
        if mail is not None:
            out.append(mail)
    return out


def message_id_of(raw: bytes) -> str:
    """The Message-ID header (``<...>``) or empty."""
    try:
        msg = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)
    except Exception:  # untrusted bytes
        return ""
    return (_MSGID.findall(_header(msg, "Message-ID")) or [""])[0]


def attachment_part(raw: bytes, part_index: int) -> bytes | None:
    """The bytes of one attachment, found again by its part number (ADR-0043 on-demand
    reads); a forwarded message comes back as its text, like at sync. None if absent."""
    try:
        msg = BytesParser(policy=policy.default).parsebytes(raw)
        for j, part in enumerate(_parts(msg)):
            if j != part_index:
                continue
            if part.get_content_type() == "message/rfc822":
                return _attached_message(part)[1] or None
            payload = part.get_payload(decode=True)
            return payload if isinstance(payload, bytes) and payload else None
    except Exception:  # untrusted bytes
        return None
    return None
