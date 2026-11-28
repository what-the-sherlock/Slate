"""An in-memory IMAP server: folders with UIDVALIDITY and messages; read-only like the real
adapter. Credentials are checked, and every session is recorded."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime

from core.domain.errors import PermissionDenied, UpstreamUnavailable
from core.domain.mail import Folder, FolderRole, Mail, Provider, thread_key


@dataclass
class FakeFolder:
    uidvalidity: int = 1
    mails: list[Mail] = field(default_factory=list[Mail])
    next_uid: int = 1


class FakeMailSession:
    def __init__(self, box: FakeMailbox) -> None:
        self._box = box
        self._folder: str | None = None

    async def folders(self) -> list[Folder]:
        roles = {"INBOX": FolderRole.INBOX}
        return [Folder(n, roles.get(n, FolderRole.SENT)) for n in self._box.folders]

    async def open_folder(self, name: str) -> int:
        if name not in self._box.folders:
            raise UpstreamUnavailable("The mail server refused to open a folder.")
        self._folder = name
        return self._box.folders[name].uidvalidity

    def _current(self) -> FakeFolder:
        assert self._folder is not None
        return self._box.folders[self._folder]

    async def uids_since(self, since: datetime) -> list[int]:
        return sorted(m.uid for m in self._current().mails if m.date.date() >= since.date())

    async def uids_after(self, uid: int) -> list[int]:
        return sorted(m.uid for m in self._current().mails if m.uid > uid)

    async def fetch(self, uids: Sequence[int]) -> list[Mail]:
        if self._box.down:
            raise UpstreamUnavailable("The mail server is unavailable right now.")
        wanted = set(uids)
        self._box.fetched += len(wanted)
        return [m for m in self._current().mails if m.uid in wanted]

    async def attachment(self, uid: int, part: int) -> tuple[str, bytes] | None:
        for m in self._current().mails:
            if m.uid != uid:
                continue
            for b in m.blobs:
                if b.info.ref.get("part") == str(part):
                    return m.message_id, b.data
        return None

    async def thread_uids(self, mail: Mail) -> list[int]:
        key = thread_key(mail)
        return sorted(m.uid for m in self._current().mails if thread_key(m) == key)


class FakeMailbox:
    def __init__(self, *, password: str = "abcdefghijklmnop") -> None:  # noqa: S107 - test fake
        self.password = password
        self.folders: dict[str, FakeFolder] = {"INBOX": FakeFolder(), "Sent": FakeFolder()}
        self.logins: list[tuple[Provider, str]] = []
        self.down = False
        self.fetched = 0

    def deliver(self, folder: str, mail: Mail) -> Mail:
        f = self.folders[folder]
        stored = replace(mail, uid=f.next_uid, folder=folder)
        f.next_uid += 1
        f.mails.append(stored)
        return stored

    @asynccontextmanager
    async def session(
        self, provider: Provider, address: str, password: str
    ) -> AsyncIterator[FakeMailSession]:
        self.logins.append((provider, address))
        if password != self.password:
            raise PermissionDenied("The mail server didn't accept that address and app password.")
        yield FakeMailSession(self)
