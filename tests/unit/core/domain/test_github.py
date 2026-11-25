from __future__ import annotations

from datetime import UTC, datetime

import pytest

from core.domain.github import (
    MAX_ISSUE_CHARS,
    Comment,
    Issue,
    SyncCheckpoint,
    is_doc_path,
    is_valid_repo_name,
    issue_external_id,
    issue_markdown,
    mime_for,
)

T = datetime(2026, 11, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    ("path", "ok"),
    [
        ("README.md", True),
        ("docs/guide/setup.mdx", True),
        ("CHANGELOG", True),
        ("notes.txt", True),
        (".github/CONTRIBUTING.md", True),
        ("src/app.py", False),
        ("node_modules/pkg/README.md", False),
        ("vendor/lib/doc.md", False),
        (".hidden/notes.md", False),
        ("../escape.md", False),
        ("/abs/readme.md", False),
        ("image.png", False),
    ],
)
def test_is_doc_path(path: str, ok: bool) -> None:
    assert is_doc_path(path) is ok


@pytest.mark.parametrize(
    ("name", "ok"),
    [
        ("octo/hello-world", True),
        ("a/b.c_d", True),
        ("octo", False),
        ("a/../b", False),
        ("-x/y", False),
    ],
)
def test_repo_names(name: str, ok: bool) -> None:
    assert is_valid_repo_name(name) is ok


def test_mime_and_ids() -> None:
    assert mime_for("a/b.MD") == "text/markdown"
    assert mime_for("LICENSE") == "text/plain"
    assert issue_external_id(7, is_pr=True) == "pr:7"
    assert issue_external_id(7, is_pr=False) == "issue:7"


def test_issue_markdown_has_header_body_and_comments() -> None:
    issue = Issue(
        12,
        "Fix login",
        "Steps to reproduce",
        "closed",
        True,
        "ada",
        "u",
        T,
        T,
        1,
        ("bug",),
        merged=True,
    )
    text = issue_markdown(issue, [Comment("bob", "LGTM", T), Comment("eve", "   ", T)])
    assert text.startswith("# Pull request #12: Fix login")
    assert "Status: merged" in text
    assert "Labels: bug" in text
    assert "## Comment by bob" in text
    assert "eve" not in text  # empty comments are skipped


def test_issue_markdown_is_capped() -> None:
    issue = Issue(1, "Big", "x" * (MAX_ISSUE_CHARS + 10), "open", False, None, "u", T, T)
    assert len(issue_markdown(issue, [])) <= MAX_ISSUE_CHARS + 20


def test_checkpoint_round_trip_and_garbage() -> None:
    cp = SyncCheckpoint("abc", "2026-11-01T00:00:00Z")
    assert SyncCheckpoint.from_json(cp.as_json()) == cp
    assert SyncCheckpoint.from_json(None) == SyncCheckpoint()
    assert SyncCheckpoint.from_json({"commit": 5}) == SyncCheckpoint()
