"""Attachments at the edges (ADR-0043): MIME parsing, Gmail, Discord's CDN. No network."""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from email.message import EmailMessage
from typing import Any

import httpx
import pytest

from core.adapters.discord.api import DiscordBot, parse_message
from core.adapters.google.gmail import CHECKPOINT_VERSION, GmailConnector
from core.adapters.mail.parse import attachment_part, message_id_of, parse_mail, parse_mbox
from core.domain.attachments import MAX_BYTES_PER_PAGE, AttachmentState, key_seed
from core.domain.errors import UnreadableFile
from core.domain.mail import (
    MAIL_CHECKPOINT_VERSION,
    MailCheckpoint,
    cap_attachment_bytes,
    dedupe,
    thread_files,
    thread_metadata,
)

NOW = datetime(2026, 11, 28, 12, 0, tzinfo=UTC)


def email(*, subject: str = "Test", body: str = "", msgid: str = "<t1@x>") -> EmailMessage:
    m = EmailMessage()
    m["From"] = "Me <me@x.com>"
    m["To"] = "Aashutosh <a@y.com>"
    m["Subject"] = subject
    m["Message-ID"] = msgid
    m["Date"] = "Sat, 28 Nov 2026 14:05:00 +0000"
    m.set_content(body)
    return m


# ---------------------------------------------------------------- MIME


def test_a_real_attachment_is_listed_with_type_size_and_kept_bytes() -> None:
    m = email()
    m.add_attachment(b"%PDF-1.4 resume", maintype="application", subtype="pdf", filename="cv.pdf")
    mail = parse_mail(m.as_bytes(), uid=7, folder="INBOX", keep_attachments=True)
    assert mail is not None
    [f] = mail.files
    assert (f.name, f.mime, f.size, f.state) == (
        "cv.pdf",
        "application/pdf",
        15,
        AttachmentState.LISTED,
    )
    assert f.key.startswith(key_seed("<t1@x>") + "-")
    assert f.ref == {"part": f.ref["part"], "folder": "INBOX", "uid": "7"}
    assert mail.blobs[0].data == b"%PDF-1.4 resume"


def test_bytes_are_kept_only_when_asked() -> None:
    m = email()
    m.add_attachment(b"%PDF", maintype="application", subtype="pdf", filename="cv.pdf")
    mail = parse_mail(m.as_bytes(), uid=1, folder="INBOX")
    assert mail is not None
    assert mail.files
    assert mail.blobs == ()


def test_inline_images_and_signature_logos_are_not_attachments() -> None:  # edge case 8
    m = email(body="hi")
    m.add_attachment(b"\x89PNG", maintype="image", subtype="png", filename="logo.png",
                     disposition="inline", cid="<logo>")  # fmt: skip
    m.add_attachment(b"\x89PNG", maintype="image", subtype="png", filename="photo.png")
    mail = parse_mail(m.as_bytes(), uid=1, folder="INBOX", keep_attachments=True)
    assert mail is not None
    assert [f.name for f in mail.files] == ["photo.png"]


def test_a_forwarded_message_is_one_attachment_read_as_its_text() -> None:  # edge case 9
    inner = email(subject="Contract terms", body="Net 30 payment.", msgid="<in@x>")
    m = email(body="see below")
    m.add_attachment(inner)
    mail = parse_mail(m.as_bytes(), uid=1, folder="INBOX", keep_attachments=True)
    assert mail is not None
    assert [f.name for f in mail.files] == ["Contract terms.eml"]
    assert mail.body.strip() == "see below"  # the inner body is not the outer body
    assert b"Net 30 payment." in mail.blobs[0].data


def test_encoded_and_non_ascii_file_names_are_decoded_and_cleaned() -> None:  # edge case 10
    m = email()
    m.add_attachment(b"x", maintype="text", subtype="plain", filename="Résumé\u0007 2026.txt")
    mail = parse_mail(m.as_bytes(), uid=1, folder="INBOX")
    assert mail is not None
    assert mail.files[0].name == "Résumé 2026.txt"


def test_a_path_in_a_file_name_is_dropped() -> None:
    m = email()
    m.add_attachment(b"x", maintype="text", subtype="plain", filename="../../etc/passwd")
    mail = parse_mail(m.as_bytes(), uid=1, folder="INBOX")
    assert mail is not None
    assert mail.files[0].name == "passwd"


def test_an_attachment_can_be_found_again_by_part_number() -> None:
    m = email()
    m.add_attachment(b"alpha", maintype="text", subtype="plain", filename="a.txt")
    m.add_attachment(b"beta", maintype="text", subtype="plain", filename="b.txt")
    raw = m.as_bytes()
    mail = parse_mail(raw, uid=1, folder="INBOX")
    assert mail is not None
    parts = [int(f.ref["part"]) for f in mail.files]
    assert [attachment_part(raw, p) for p in parts] == [b"alpha", b"beta"]
    assert attachment_part(raw, 99) is None
    assert message_id_of(raw) == "<t1@x>"


def test_takeout_sent_label_marks_sent_mail() -> None:
    m = email()
    m["X-Gmail-Labels"] = "Opened,Sent"
    mbox = b"From x@y Sat Nov 28 14:05:00 2026\n" + m.as_bytes() + b"\n"
    [mail] = parse_mbox(mbox)
    assert mail.sent


def test_a_message_in_both_inbox_and_sent_counts_as_sent() -> None:
    m = email()
    a = parse_mail(m.as_bytes(), uid=1, folder="INBOX")
    b = parse_mail(m.as_bytes(), uid=2, folder="Sent")
    assert a
    assert b
    from dataclasses import replace

    [merged] = dedupe([a, replace(b, sent=True)])
    assert merged.sent


def test_thread_metadata_names_people_folders_and_files() -> None:
    m = email()
    m.add_attachment(b"%PDF", maintype="application", subtype="pdf", filename="cv.pdf")
    mail = parse_mail(m.as_bytes(), uid=1, folder="Sent", keep_attachments=True)
    assert mail is not None
    from dataclasses import replace

    meta = thread_metadata([replace(mail, sent=True)])
    assert meta["folders"] == ["sent"]
    assert "Aashutosh <a@y.com>" in meta["people"]  # type: ignore[operator]
    assert meta["recipients"] == "Aashutosh <a@y.com>"
    assert [a["name"] for a in meta["attachments"]] == ["cv.pdf"]  # type: ignore[union-attr]
    files, blobs = thread_files([mail])
    assert len(files) == len(blobs) == 1


def test_a_runs_attachment_memory_is_capped_newest_first() -> None:
    import core.domain.mail as mail_mod

    msgs = []
    for i in range(3):
        m = email(msgid=f"<m{i}@x>")
        m.replace_header("Date", f"Sat, 2{i} Nov 2026 10:00:00 +0000")
        m.add_attachment(b"x" * 1000, maintype="text", subtype="plain", filename=f"{i}.txt")
        parsed = parse_mail(m.as_bytes(), uid=i, folder="INBOX", keep_attachments=True)
        assert parsed is not None
        msgs.append(parsed)
    old = mail_mod.MAX_ATTACHMENT_BYTES_PER_RUN
    mail_mod.MAX_ATTACHMENT_BYTES_PER_RUN = 2000
    try:
        capped = cap_attachment_bytes(msgs)
    finally:
        mail_mod.MAX_ATTACHMENT_BYTES_PER_RUN = old
    kept = {m.message_id: len(m.blobs) for m in capped}
    assert kept == {"<m2@x>": 1, "<m1@x>": 1, "<m0@x>": 0}  # the oldest waits for on demand
    assert all(m.files for m in capped)  # still listed


def test_one_messages_kept_bytes_are_capped() -> None:
    m = email()
    m.add_attachment(b"x" * 100, maintype="text", subtype="plain", filename="a.txt")
    mail = parse_mail(m.as_bytes(), uid=1, folder="I", keep_attachments=True)
    assert mail is not None
    assert MAX_BYTES_PER_PAGE > 100
    assert mail.blobs


def test_an_old_imap_checkpoint_rereads_once() -> None:
    old = {"folders": {"INBOX": {"uidvalidity": 1, "last_uid": 50}}}
    assert MailCheckpoint.from_json(old).first_sync
    new = MailCheckpoint.from_json({**old, "v": MAIL_CHECKPOINT_VERSION})
    assert not new.first_sync
    assert MailCheckpoint.from_json(new.as_json()).cursor("INBOX").last_uid == 50


# ---------------------------------------------------------------- Gmail


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def gmail(routes: dict[str, Any]) -> tuple[GmailConnector, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handle(r: httpx.Request) -> httpx.Response:
        seen.append(r)
        for suffix, body in routes.items():
            if r.url.path.endswith(suffix):
                return body if isinstance(body, httpx.Response) else httpx.Response(200, json=body)
        return httpx.Response(404, json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    return GmailConnector(client, sleep=0), seen


async def test_an_old_gmail_checkpoint_starts_one_backfill() -> None:
    conn, seen = gmail({"/profile": {"historyId": "9"}, "/threads": {"threads": []}})
    page = await conn.pull("tok", {"phase": "incremental", "history_id": "5"}, now=NOW)
    assert any(r.url.path.endswith("/threads") for r in seen)
    assert page.checkpoint["v"] == CHECKPOINT_VERSION


async def test_gmail_hands_over_attachment_bytes_and_marks_sent_mail() -> None:
    m = email(body="")
    m.add_attachment(b"Q1 plan", maintype="text", subtype="plain", filename="plan.txt")
    conn, _ = gmail({
        "/profile": {"historyId": "9"},
        "/threads": {"threads": [{"id": "t1"}]},
        "/threads/t1": {"messages": [{"id": "m1", "labelIds": ["SENT"], "sizeEstimate": 500}]},
        "/messages/m1": {"raw": b64(m.as_bytes())},
    })  # fmt: skip
    page = await conn.pull("tok", {}, now=NOW)
    [doc] = page.documents
    assert doc.metadata["folders"] == ["sent"]
    [blob] = doc.attachments
    assert blob.data == b"Q1 plan"
    assert blob.info.ref["message"] == "m1"
    assert "[attachment: plan.txt (TXT, 7 B)]" in doc.sections[0].text


async def test_a_message_over_10_mb_is_indexed_not_skipped() -> None:  # edge case 14
    payload = {
        "headers": [
            {"name": "From", "value": "Me <me@x.com>"}, {"name": "To", "value": "a@y.com"},
            {"name": "Subject", "value": "Big deck"}, {"name": "Message-ID", "value": "<big@x>"},
        ],
        "parts": [
            {"partId": "0", "mimeType": "text/plain", "body": {"data": b64(b"Deck attached")}},
            {"partId": "1", "mimeType": "application/pdf", "filename": "deck.pdf",
             "body": {"size": 12_000_000, "attachmentId": "ATT"}},
            {"partId": "2", "mimeType": "application/pdf", "filename": "notes.pdf",
             "body": {"size": 2_000, "attachmentId": "ATT2"}},
        ],
    }  # fmt: skip
    conn, seen = gmail({
        "/profile": {"historyId": "9"},
        "/threads": {"threads": [{"id": "t1"}]},
        "/threads/t1": {"messages": [{"id": "m1", "labelIds": ["INBOX"],
                                      "sizeEstimate": 14_000_000}]},
        "/messages/m1": {"payload": payload},
    })  # fmt: skip
    page = await conn.pull("tok", {}, now=NOW)
    [doc] = page.documents
    assert "Deck attached" in doc.sections[0].text
    names = [a["name"] for a in doc.metadata["attachments"]]  # type: ignore[union-attr]
    assert names == ["deck.pdf", "notes.pdf"]
    assert doc.attachments == ()  # nothing downloaded at sync
    assert any("format=full" in str(r.url) for r in seen)


async def test_a_large_messages_file_is_fetched_alone_on_demand() -> None:
    payload = {"parts": [{"partId": "2", "mimeType": "application/pdf", "filename": "n.pdf",
                          "body": {"size": 4, "attachmentId": "ATT-2"}}]}  # fmt: skip
    conn, _seen = gmail({
        "/messages/m1": {"payload": payload},
        "/attachments/ATT-2": {"data": b64(b"%PDF")},
    })  # fmt: skip
    assert await conn.fetch_attachment("tok", {"message": "m1", "gpart": "2"}) == b"%PDF"
    assert await conn.fetch_attachment("tok", {"message": "m1", "gpart": "9"}) is None
    assert await conn.fetch_attachment("tok", {"message": "../x", "gpart": "2"}) is None


async def test_a_deleted_gmail_message_means_gone() -> None:
    conn, _ = gmail({})
    assert await conn.fetch_attachment("tok", {"message": "m1", "part": "1"}) is None


# ---------------------------------------------------------------- Discord


def test_discord_messages_keep_file_ids_sizes_and_short_lived_links() -> None:
    m = parse_message({
        "id": "1100000000000000001", "channel_id": "1200000000000000001", "content": "",
        "author": {"id": "1300000000000000001", "username": "aashutosh"},
        "attachments": [{"id": "1400000000000000001", "filename": "call.docx", "size": 2048,
                         "content_type": "application/vnd.openxmlformats-officedocument."
                                         "wordprocessingml.document",
                         "url": "https://cdn.discordapp.com/attachments/a/b/call.docx?ex=1"}],
    })  # fmt: skip
    [f] = m.files
    assert (f.key, f.name, f.size) == ("1400000000000000001", "call.docx", 2048)
    assert f.ref["message"] == "1100000000000000001"
    assert "url" not in f.to_meta()
    assert "cdn" not in str(f.to_meta())
    assert m.file_urls[f.key].startswith("https://cdn.discordapp.com/")


@pytest.mark.parametrize(
    "url",
    [
        "http://cdn.discordapp.com/a.pdf",  # not https
        "https://evil.example/a.pdf",  # not Discord's CDN
        "https://cdn.discordapp.com.evil.example/a.pdf",
        "https://cdn.discordapp.com:8443/a.pdf",
    ],
)
async def test_downloads_only_come_from_discords_cdn(url: str) -> None:  # SSRF guard
    def fail(r: httpx.Request) -> httpx.Response:
        raise AssertionError("no request may be sent")

    bot = DiscordBot(bot_token="t", client_id=None, client_secret=None, redirect_uri=None,
                     client=httpx.AsyncClient(transport=httpx.MockTransport(fail)))  # fmt: skip
    assert await bot.download(url, 100) is None


async def test_downloads_are_byte_capped_and_gone_files_are_none() -> None:
    def handle(r: httpx.Request) -> httpx.Response:
        if r.url.path.endswith("big.pdf"):
            return httpx.Response(200, content=b"x" * 500)
        if r.url.path.endswith("gone.pdf"):
            return httpx.Response(404)
        assert "authorization" not in {k.lower() for k in r.headers}  # the link is signed
        return httpx.Response(200, content=b"%PDF")

    bot = DiscordBot(bot_token="t", client_id=None, client_secret=None, redirect_uri=None,
                     client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))  # fmt: skip
    base = "https://cdn.discordapp.com/attachments/1/2/"
    assert await bot.download(base + "ok.pdf", 100) == b"%PDF"
    assert await bot.download(base + "gone.pdf", 100) is None
    with pytest.raises(UnreadableFile) as e:
        await bot.download(base + "big.pdf", 100)
    assert e.value.reason == "too_large"
