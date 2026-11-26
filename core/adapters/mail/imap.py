"""Mailbox over IMAP (ADR-0028): stdlib ``imaplib``, blocking calls run in a worker thread.

Read-only by construction: folders are opened with EXAMINE (``readonly=True``), messages are
fetched with ``BODY.PEEK[]`` (no \\Seen flag is set), and no STORE, COPY, MOVE, EXPUNGE or
APPEND command exists in this module. Only hosts from ``core.domain.mail.PROVIDERS`` are
contacted, over TLS with certificate and hostname checks. Every socket operation has a
timeout. The app password is used for LOGIN only and never logged.
"""

from __future__ import annotations

import asyncio
import imaplib
import re
import ssl
from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from typing import TypeVar, cast

import structlog

from core.adapters.mail.parse import parse_mail
from core.domain.errors import PermissionDenied, UpstreamUnavailable
from core.domain.mail import (
    FETCH_BATCH,
    MAX_RAW_MESSAGE_BYTES,
    PROVIDERS,
    Folder,
    Mail,
    Provider,
    pick_folders,
)

log = structlog.get_logger(__name__)

TIMEOUT_S = 30.0
T = TypeVar("T")
_LIST = re.compile(rb'^\((?P<flags>[^)]*)\) (?:"[^"]*"|NIL) (?P<name>.+)$')
_META = re.compile(rb"UID (\d+)|X-GM-THRID (\d+)|RFC822\.SIZE (\d+)|INTERNALDATE \"([^\"]+)\"")
# IMAP commands are ASCII: a non-ASCII Message-ID (anyone can send one) must not reach SEARCH.
_SAFE_ID = re.compile(r"^<[!#-;=?-\[\]-~]{1,500}>$")  # printable ASCII except " < > \\

LOGIN_REJECTED = (
    "The mail server didn't accept that address and app password. Check both, and that IMAP "
    "is turned on for the account."
)


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_INTERNALDATE = re.compile(
    r"^\s?(\d{1,2})-([A-Za-z]{3})-(\d{4}) (\d{2}):(\d{2}):(\d{2}) ([+-])(\d{2})(\d{2})$"
)


def imap_date(when: datetime) -> str:
    """RFC 3501 date ("20-Nov-2026"), independent of the process locale."""
    return f"{when.day}-{_MONTHS[when.month - 1]}-{when.year}"


def parse_internaldate(raw: str) -> datetime | None:
    m = _INTERNALDATE.match(raw)
    if m is None or m.group(2).title() not in _MONTHS:
        return None
    sign = 1 if m.group(7) == "+" else -1
    offset = timedelta(hours=int(m.group(8)), minutes=int(m.group(9))) * sign
    try:
        return datetime(
            int(m.group(3)), _MONTHS.index(m.group(2).title()) + 1, int(m.group(1)),
            int(m.group(4)), int(m.group(5)), int(m.group(6)), tzinfo=timezone(offset),
        )  # fmt: skip
    except ValueError:
        return None


def _quote(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _unquote(raw: bytes) -> str:
    text = raw.decode("utf-8", "replace").strip()
    if text.startswith('"') and text.endswith('"'):
        text = text[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return text


def parse_list(
    lines: Sequence[bytes | tuple[bytes, bytes] | None],
) -> list[tuple[str, frozenset[str]]]:
    out: list[tuple[str, frozenset[str]]] = []
    for line in lines:
        if not isinstance(line, bytes):
            continue
        m = _LIST.match(line.strip())
        if m is None:
            continue
        flags = frozenset(f.decode(errors="replace") for f in m.group("flags").split())
        if "\\Noselect" in flags or "\\NonExistent" in flags:
            continue
        out.append((_unquote(m.group("name")), flags))
    return out


def _rows(data: object) -> list[bytes | tuple[object, ...] | None]:
    """imaplib's untyped response list, narrowed: each row is bytes, a (head, body) tuple or
    None."""
    rows = cast(list[object], data) if isinstance(data, list) else []
    out: list[bytes | tuple[object, ...] | None] = []
    for row in rows:
        if isinstance(row, bytes):
            out.append(row)
        elif isinstance(row, tuple):
            out.append(cast(tuple[object, ...], row))
    return out


def _uids(data: Sequence[bytes | None]) -> list[int]:
    found: set[int] = set()
    for chunk in data:
        if isinstance(chunk, bytes):
            found.update(int(x) for x in chunk.split() if x.isdigit())
    return sorted(found)


def _meta(header: bytes) -> tuple[int | None, str | None, int | None, datetime | None]:
    uid = thrid = size = None
    when: datetime | None = None
    for m in _META.finditer(header):
        if m.group(1):
            uid = int(m.group(1))
        elif m.group(2):
            thrid = m.group(2).decode()
        elif m.group(3):
            size = int(m.group(3))
        elif m.group(4):
            when = parse_internaldate(m.group(4).decode())
    return uid, thrid, size, when


class ImapSession:
    def __init__(self, conn: imaplib.IMAP4_SSL, *, gmail: bool) -> None:
        self._conn = conn
        self._gmail = gmail
        self._folder: str | None = None

    async def _run(self, fn: Callable[[], T]) -> T:
        try:
            return await asyncio.to_thread(fn)
        except (imaplib.IMAP4.abort, OSError, ssl.SSLError) as e:
            log.warning("imap.unavailable", error=type(e).__name__)
            raise UpstreamUnavailable("The mail server is unavailable right now.") from e
        except (imaplib.IMAP4.error, UnicodeError) as e:
            log.warning("imap.command_failed", error=type(e).__name__)
            raise UpstreamUnavailable("The mail server refused a request.") from e

    def _ok(self, typ: str, what: str) -> None:
        if typ != "OK":
            raise UpstreamUnavailable(f"The mail server refused to {what}.")

    async def folders(self) -> list[Folder]:
        typ, lines = await self._run(lambda: self._conn.list())
        self._ok(typ, "list folders")
        return pick_folders(parse_list(lines))

    async def open_folder(self, name: str) -> int:
        def go() -> int:
            typ, _ = self._conn.select(_quote(name), readonly=True)  # EXAMINE: read-only
            self._ok(typ, "open a folder")
            _, data = self._conn.response("UIDVALIDITY")
            values = [int(x) for x in data if isinstance(x, bytes) and x.isdigit()]
            if not values:
                raise UpstreamUnavailable("The mail server didn't report UIDVALIDITY.")
            return values[0]

        uidvalidity = await self._run(go)
        self._folder = name
        return uidvalidity

    async def _search(self, *criteria: str) -> list[int]:
        typ, data = await self._run(lambda: self._conn.uid("SEARCH", *criteria))
        self._ok(typ, "search")
        return _uids(data)

    async def uids_since(self, since: datetime) -> list[int]:
        return await self._search("SINCE", imap_date(since))

    async def uids_after(self, uid: int) -> list[int]:
        # "UID n:*" always returns the newest message even when n is past it: filter.
        return [u for u in await self._search("UID", f"{uid + 1}:*") if u > uid]

    async def thread_uids(self, mail: Mail) -> list[int]:
        if self._gmail and mail.gm_thread and mail.gm_thread.isdigit():
            return await self._search("X-GM-THRID", mail.gm_thread)
        root = mail.references[0] if mail.references else mail.message_id
        if not _SAFE_ID.match(root):
            return [mail.uid]
        quoted = _quote(root)
        return await self._search(
            "OR", "HEADER", "Message-ID", quoted, "HEADER", "References", quoted
        )

    async def fetch(self, uids: Sequence[int]) -> list[Mail]:
        out: list[Mail] = []
        folder = self._folder or "INBOX"
        for i in range(0, len(uids), FETCH_BATCH):
            batch = ",".join(str(u) for u in uids[i : i + FETCH_BATCH])
            meta_items = (
                "(UID RFC822.SIZE INTERNALDATE X-GM-THRID)"
                if self._gmail
                else "(UID RFC822.SIZE INTERNALDATE)"
            )
            typ, data = await self._run(lambda b=batch, m=meta_items: self._conn.uid("FETCH", b, m))
            self._ok(typ, "read messages")
            small: list[int] = []
            info: dict[int, tuple[str | None, datetime | None]] = {}
            for item in _rows(data):
                head = item[0] if isinstance(item, tuple) else item
                if not isinstance(head, bytes):
                    continue
                uid, thrid, size, when = _meta(head)
                if uid is None:
                    continue
                info[uid] = (thrid, when)
                if size is None or size <= MAX_RAW_MESSAGE_BYTES:
                    small.append(uid)
            if not small:
                continue
            wanted = ",".join(str(u) for u in small)
            typ, data = await self._run(
                lambda w=wanted: self._conn.uid("FETCH", w, "(UID BODY.PEEK[])")
            )
            self._ok(typ, "read messages")
            for item in _rows(data):
                if not isinstance(item, tuple) or len(item) < 2:
                    continue
                head, body = item[0], item[1]
                if not isinstance(head, bytes) or not isinstance(body, bytes):
                    continue
                uid, _, _, _ = _meta(head)
                if uid is None:
                    continue
                thrid, when = info.get(uid, (None, None))
                mail = parse_mail(body, uid=uid, folder=folder, gm_thread=thrid, internal_date=when)
                if mail is not None:
                    out.append(mail)
        return out


class ImapMailbox:
    def __init__(self, *, timeout_s: float = TIMEOUT_S) -> None:
        self._timeout = timeout_s

    @asynccontextmanager
    async def session(
        self, provider: Provider, address: str, password: str
    ) -> AsyncGenerator[ImapSession]:
        info = PROVIDERS[provider]  # never a user-supplied host

        def connect() -> imaplib.IMAP4_SSL:
            ctx = ssl.create_default_context()  # verifies certificate and hostname
            conn = imaplib.IMAP4_SSL(info.host, info.port, ssl_context=ctx, timeout=self._timeout)
            try:
                conn.login(address, password)
            except BaseException:
                conn.shutdown()  # never leak the socket, whatever went wrong
                raise
            return conn

        try:
            conn = await asyncio.to_thread(connect)
        except (imaplib.IMAP4.error, UnicodeError):
            log.info("imap.login_rejected", provider=provider.value)
            raise PermissionDenied(
                LOGIN_REJECTED
            ) from None  # no chained exception: it holds the password
        except (TimeoutError, OSError, ssl.SSLError) as e:
            log.warning("imap.connect_failed", provider=provider.value, error=type(e).__name__)
            raise UpstreamUnavailable(f"Couldn't reach {info.name} right now.") from e
        gmail = provider is Provider.GMAIL or "X-GM-EXT-1" in conn.capabilities
        try:
            yield ImapSession(conn, gmail=gmail)
        finally:
            with suppress(imaplib.IMAP4.error, OSError):
                await asyncio.to_thread(conn.logout)
