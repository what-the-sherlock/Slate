"""On-demand fetchers (ADR-0043): Discord (fresh links, rebuilt windows) and IMAP
(Message-ID integrity), against the in-memory fakes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from uuid import uuid4

from core.adapters.mail.parse import parse_mail
from core.domain.attachments import AttachmentInfo, key_seed
from core.domain.discord import Channel, Guild, Message, snowflake_at
from core.domain.documents import AttachmentParent
from core.domain.ids import DocumentId, SourceId, WorkspaceId
from core.domain.mail import Provider
from core.services.attachment_fetchers import DiscordFiles, MailboxFiles
from tests.fakes.discord import FakeDiscord
from tests.fakes.mailbox import FakeMailbox

WS = WorkspaceId(uuid4())
T0 = datetime(2026, 11, 21, 10, 0, tzinfo=UTC)
CH = "1200000000000000001"


def snow(at: datetime) -> str:
    return str(snowflake_at(at))


def msg(at: datetime, *files: tuple[str, str]) -> Message:
    mid = snow(at)
    infos = tuple(
        AttachmentInfo(key=fid, name=name, mime="text/plain", size=4,
                       ref={"channel": CH, "message": mid, "attachment": fid})
        for fid, name in files
    )  # fmt: skip
    return Message(
        id=mid, channel_id=CH, author_id="1", author_name="Aashutosh", content="see file",
        attachments=tuple(n for _, n in files), files=infos,
        file_urls={fid: f"https://cdn.discordapp.com/{fid}/{name}" for fid, name in files},
    )  # fmt: skip


def parent(external_id: str, attachments: tuple[AttachmentInfo, ...] = ()) -> AttachmentParent:
    return AttachmentParent(
        id=DocumentId(uuid4()), workspace_id=WS, source_id=SourceId(uuid4()), connector="discord",
        external_id=external_id, title="t", uri="u", author=None, updated_at=None,
        data_class="private", acl=("user:a",), attachments=attachments,
    )  # fmt: skip


def discord_with(*messages: Message) -> FakeDiscord:
    dc = FakeDiscord()
    server = dc.add_server(Guild("1000000000000000001", "g", "1", ()))
    server.channels[CH] = Channel(CH, "general", 0)
    dc.post(CH, *messages)
    return dc


async def test_discord_fetch_uses_a_fresh_link_from_the_message() -> None:
    m = msg(T0, ("1400000000000000001", "call.docx"))
    dc = discord_with(m)
    dc.cdn[m.file_urls["1400000000000000001"]] = b"docx bytes"
    got = await DiscordFiles(dc).fetch(parent(f"msg:{CH}:{m.id}"), m.files[0])
    assert got == b"docx bytes"
    assert dc.downloads == [m.file_urls["1400000000000000001"]]


async def test_a_deleted_message_or_file_is_gone() -> None:  # edge case 13
    m = msg(T0, ("1400000000000000001", "call.docx"))
    dc = discord_with()  # the message no longer exists
    assert await DiscordFiles(dc).fetch(parent(f"msg:{CH}:{m.id}"), m.files[0]) is None
    other = msg(T0, ("1400000000000000009", "other.docx"))
    dc2 = discord_with(other)  # same message id, but the file was removed from it
    assert await DiscordFiles(dc2).fetch(parent(f"msg:{CH}:{m.id}"), m.files[0]) is None


async def test_a_channel_the_bot_lost_is_gone() -> None:  # edge case 13
    m = msg(T0, ("1400000000000000001", "call.docx"))
    dc = discord_with(m)
    dc.forbidden_channels.add(CH)
    assert await DiscordFiles(dc).fetch(parent(f"msg:{CH}:{m.id}"), m.files[0]) is None


async def test_bad_refs_never_reach_discord() -> None:
    dc = discord_with()
    bad = AttachmentInfo(key="k", name="a", mime="x", size=1, ref={"channel": "../x"})
    assert await DiscordFiles(dc).fetch(parent(f"msg:{CH}:1"), bad) is None
    assert dc.message_calls == 0


async def test_an_old_window_is_rebuilt_exactly_to_find_its_files() -> None:
    first = msg(T0, ("1400000000000000001", "call.docx"))
    same = msg(T0 + timedelta(minutes=5), ("1400000000000000002", "notes.txt"))
    later = msg(T0 + timedelta(hours=3), ("1400000000000000003", "next-window.txt"))
    dc = discord_with(first, same, later)
    found = await DiscordFiles(dc).discover(parent(f"msg:{CH}:{first.id}"))
    assert [f.name for f in found] == ["call.docx", "notes.txt"]  # not the next window's


async def test_discover_ignores_documents_it_cant_parse() -> None:
    dc = discord_with()
    assert await DiscordFiles(dc).discover(parent("upload:x")) == []


# ---------------------------------------------------------------- IMAP


def mailbox_with_attachment(msgid: str) -> tuple[FakeMailbox, AttachmentInfo]:
    box = FakeMailbox()
    m = EmailMessage()
    m["From"], m["Subject"], m["Message-ID"] = "a@x", "s", msgid
    m.set_content("hi")
    m.add_attachment(b"alpha", maintype="text", subtype="plain", filename="a.txt")
    mail = parse_mail(m.as_bytes(), uid=1, folder="INBOX", keep_attachments=True)
    assert mail is not None
    stored = box.deliver("INBOX", mail)
    return box, stored.files[0]


def test_the_message_id_guard_matches_the_key() -> None:
    _, info = mailbox_with_attachment("<orig@x>")
    assert info.key.startswith(key_seed("<orig@x>") + "-")
    assert not info.key.startswith(key_seed("<other@x>") + "-")


async def test_imap_refetch_refuses_a_different_message_under_the_same_uid() -> None:
    box, info = mailbox_with_attachment("<orig@x>")
    files = MailboxFiles(box, _Creds(), _Sources())  # type: ignore[arg-type]
    p = parent("thread:x")
    assert await files.fetch(p, info) == b"alpha"
    # The mailbox was rebuilt: UID 1 is now someone else's message.
    box.folders["INBOX"].mails.clear()
    box.folders["INBOX"].next_uid = 1
    other, _ = mailbox_with_attachment("<other@x>")
    box.folders["INBOX"] = other.folders["INBOX"]
    assert await files.fetch(p, info) is None


class _Creds:
    async def secret(self, ws: WorkspaceId, credential_id: object) -> str | None:
        return "abcdefghijklmnop"


class _Sources:
    async def get_source(self, ws: WorkspaceId, source_id: SourceId) -> object:
        class S:
            def __init__(self) -> None:
                self.workspace_id = ws
                self.credential_id = uuid4()
                self.config = {"provider": Provider.GMAIL.value, "address": "me@x.com"}

        return S()
