from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st

from core.domain.data_class import DataClass
from core.domain.errors import ValidationFailed
from core.domain.ids import UserId, WorkspaceId
from core.domain.principals import PUBLIC
from core.domain.signed_tokens import sign, verify
from core.domain.uploads import (
    FileKind,
    Visibility,
    access_for,
    blob_key_for,
    kind_from_filename,
    safe_filename,
    sniff_kind,
)

SECRET = b"x" * 32
NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("Report.PDF", FileKind.PDF),
        ("notes.md", FileKind.MARKDOWN),
        ("page.htm", FileKind.HTML),
        ("data.csv", FileKind.CSV),
        ("spec.docx", FileKind.DOCX),
        ("evil.exe", None),
        ("archive.zip", None),
        ("noext", None),
    ],
)
def test_kind_from_filename(name: str, kind: FileKind | None) -> None:
    assert kind_from_filename(name) is kind


@pytest.mark.parametrize(
    ("head", "claimed", "ok"),
    [
        (b"%PDF-1.7\n...", FileKind.PDF, True),
        (b"\n%PDF-1.4", FileKind.PDF, True),
        (b"<html>not a pdf", FileKind.PDF, False),  # mislabelled
        (b"PK\x03\x04....", FileKind.DOCX, True),
        (b"%PDF-1.7", FileKind.DOCX, False),
        (b"# Title\nbody", FileKind.MARKDOWN, True),
        (b"MZ\x90\x00\x03\x00", FileKind.TEXT, False),  # a Windows .exe renamed to .txt
        ("café".encode()[:-1], FileKind.TEXT, True),  # multi-byte char cut at the boundary
        (b"\xff\xfe\xfd text", FileKind.CSV, False),  # not UTF-8
    ],
)
def test_sniff_kind_checks_bytes_not_names(head: bytes, claimed: FileKind, ok: bool) -> None:
    assert (sniff_kind(head, claimed) is claimed) is ok


@pytest.mark.parametrize(
    ("raw", "safe"),
    [
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\me\\report.pdf", "report.pdf"),
        ("a\x00b\nc.txt", "abc.txt"),
        ("   ", "untitled"),
    ],
)
def test_safe_filename(raw: str, safe: str) -> None:
    assert safe_filename(raw) == safe


def test_access_plans() -> None:
    u, w = UserId(uuid4()), WorkspaceId(uuid4())
    assert access_for(Visibility.ONLY_ME, u, w).acl == (f"user:{u}",)
    assert access_for(Visibility.WORKSPACE, u, w).data_class is DataClass.PRIVATE
    public = access_for(Visibility.PUBLIC, u, w)
    assert PUBLIC in public.acl
    assert public.data_class is DataClass.PUBLIC


def test_blob_keys_are_scoped_to_the_uploader() -> None:
    w = WorkspaceId(uuid4())
    a = blob_key_for(w, UserId(uuid4()), "ab" * 32, FileKind.PDF)
    b = blob_key_for(w, UserId(uuid4()), "ab" * 32, FileKind.PDF)
    assert a != b  # same bytes, different uploaders -> different objects


# ---------------------------------------------------------------- signed tokens


def test_token_roundtrip() -> None:
    token = sign({"k": "v"}, secret=SECRET, purpose="upload", expires_at=NOW + timedelta(minutes=5))
    assert verify(token, secret=SECRET, purpose="upload", now=NOW) == {"k": "v"}


@pytest.mark.parametrize("case", ["tampered", "wrong-secret", "wrong-purpose", "expired", "junk"])
def test_token_rejections(case: str) -> None:
    token = sign({"k": "v"}, secret=SECRET, purpose="upload", expires_at=NOW + timedelta(minutes=5))
    secret, purpose, now = SECRET, "upload", NOW
    if case == "tampered":
        body, mac = token.split(".")
        token = body[:-2] + ("AA" if body[-2:] != "AA" else "BB") + "." + mac
    elif case == "wrong-secret":
        secret = b"y" * 32
    elif case == "wrong-purpose":
        purpose = "session"
    elif case == "expired":
        now = NOW + timedelta(minutes=6)
    else:
        token = "not-a-token"
    with pytest.raises(ValidationFailed):
        verify(token, secret=secret, purpose=purpose, now=now)


@given(st.text(max_size=200))
def test_verify_never_crashes_on_garbage(token: str) -> None:
    with pytest.raises(ValidationFailed):
        verify(token, secret=SECRET, purpose="upload", now=NOW)
