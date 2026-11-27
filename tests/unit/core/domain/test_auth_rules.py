from __future__ import annotations

from uuid import UUID

import pytest
from hypothesis import given
from hypothesis import strategies as st

from core.domain.auth import (
    CodePurpose,
    canonical_email,
    hash_code,
    is_disposable_email,
    is_valid_email,
    new_code,
    workspace_name_for,
)
from core.domain.emails import code_email
from core.domain.ids import UserId

KEY = b"k" * 32
U1, U2 = UserId(UUID(int=1)), UserId(UUID(int=2))


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("Ada@Acme.dev", "ada@acme.dev"),
        ("ada+news@acme.dev", "ada@acme.dev"),
        ("a.d.a@acme.dev", "a.d.a@acme.dev"),  # dots matter outside Gmail
        ("A.D.A+x@Gmail.com", "ada@gmail.com"),
        ("ada@googlemail.com", "ada@gmail.com"),
        (" ada@acme.dev ", "ada@acme.dev"),
    ],
)
def test_canonical_email(raw: str, canonical: str) -> None:
    assert canonical_email(raw) == canonical


@given(st.emails())
def test_canonical_email_is_idempotent(email: str) -> None:
    once = canonical_email(email)
    assert canonical_email(once) == once


@pytest.mark.parametrize(
    ("email", "disposable"),
    [
        ("x@mailinator.com", True),
        ("x@inbox.mailinator.com", True),  # subdomains too
        ("x@YOPMAIL.com", True),
        ("x@acme.dev", False),
        ("x@notmailinator.com", False),  # suffix match is by label, not by string
        ("x@gmail.com", False),
    ],
)
def test_is_disposable_email(email: str, disposable: bool) -> None:
    assert is_disposable_email(email) is disposable


@pytest.mark.parametrize(
    ("email", "ok"),
    [
        ("ada@acme.dev", True),
        ("ada@acme", False),
        ("ada acme.dev", False),
        ("a@b.c" + "c" * 260, False),
    ],
)
def test_is_valid_email(email: str, ok: bool) -> None:
    assert is_valid_email(email) is ok


def test_new_code_is_six_digits_with_leading_zeros_allowed() -> None:
    codes = {new_code() for _ in range(2000)}
    assert all(len(c) == 6 and c.isdigit() for c in codes)
    assert len(codes) > 1900  # random, not a counter


def test_code_hash_is_keyed_and_bound_to_user_and_purpose() -> None:
    base = hash_code(KEY, U1, CodePurpose.VERIFY_EMAIL, "123456")
    assert base == hash_code(KEY, U1, CodePurpose.VERIFY_EMAIL, " 123456 ")
    assert base != hash_code(b"other-key" * 4, U1, CodePurpose.VERIFY_EMAIL, "123456")
    assert base != hash_code(KEY, U2, CodePurpose.VERIFY_EMAIL, "123456")
    assert base != hash_code(KEY, U1, CodePurpose.RESET_PASSWORD, "123456")
    assert base != hash_code(KEY, U1, CodePurpose.VERIFY_EMAIL, "123457")


def test_workspace_name_uses_the_first_name() -> None:
    assert workspace_name_for("Ada Lovelace") == "Ada's workspace"
    assert workspace_name_for("   ") == "My workspace"


@pytest.mark.parametrize("purpose", list(CodePurpose))
def test_code_email_is_plain_text_with_the_code_and_no_link(purpose: CodePurpose) -> None:
    mail = code_email("ada@acme.dev", "Ada", "042137", purpose)
    assert mail.subject.startswith("042137 ")
    assert "042137" in mail.text
    assert "http" not in mail.text
    assert "<" not in mail.text
    assert "15 minutes" in mail.text


@pytest.mark.parametrize("email", ["+x@corp.com", "josé@corp.com", "ada@corpé.com"])
def test_emails_that_cannot_be_delivered_or_canonicalised_are_invalid(email: str) -> None:
    assert not is_valid_email(email)


def test_code_email_has_a_branded_html_part() -> None:
    mail = code_email(
        "ada@acme.dev",
        "<b>Ada</b>",
        "042137",
        CodePurpose.VERIFY_EMAIL,
        app_url="https://slate.test/",
    )
    assert mail.html is not None
    assert "042137" in mail.html
    assert "Verify your email" in mail.html
    assert 'src="https://slate.test/brand/email-logo.png"' in mail.html
    assert "<b>Ada</b>" not in mail.html  # typed names are escaped
    assert "&lt;b&gt;Ada&lt;/b&gt;" in mail.html
    assert "<a " not in mail.html  # code emails carry no links to click
    assert "042137" in mail.text  # the plain-text part is still there


def test_code_email_without_app_url_has_no_image() -> None:
    mail = code_email("ada@acme.dev", "Ada", "042137", CodePurpose.RESET_PASSWORD)
    assert mail.html is not None
    assert "<img" not in mail.html
    assert "Reset your password" in mail.html


def test_invite_html_escapes_and_links() -> None:
    from core.domain.emails import invite_email_html

    page = invite_email_html(
        inviter='Eve "x"', workspace="<script>", role="member",
        link="https://slate.test/invite#tok&x", days=7, app_url=None,
    )  # fmt: skip
    assert "<script>" not in page
    assert 'href="https://slate.test/invite#tok&amp;x"' in page
