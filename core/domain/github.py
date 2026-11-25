"""GitHub connector rules (ADR-0023). Pure: no I/O.

What Slate indexes from a repo (owner decision): documentation files and issues/PRs with
their comments. Code is skipped in M4: embedding a whole codebase would use a workspace's
monthly AI allowance in one sync.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePosixPath

DOC_SUFFIXES = frozenset({".md", ".markdown", ".mdx", ".txt", ".rst"})
DOC_NAMES = frozenset(
    {"readme", "contributing", "changelog", "license", "security", "code_of_conduct"}
)
SKIP_DIRS = frozenset(
    {
        "node_modules",
        "vendor",
        "third_party",
        "dist",
        "build",
        ".git",
        ".venv",
        "venv",
        "site-packages",
        "target",
    }
)
MAX_DOC_FILE_BYTES = 256 * 1024
MAX_DOC_FILES = 500  # per repo; the rest is skipped and reported
MAX_TARBALL_BYTES = 50 * 1024 * 1024
MAX_TAR_MEMBERS = 50_000  # entries walked, docs or not
MAX_UNPACKED_BYTES = 200 * 1024 * 1024  # declared sizes, summed
MAX_ISSUES_PER_SYNC = 300  # the next hourly sync continues from the checkpoint
MAX_COMMENTS = 100
MAX_ISSUE_CHARS = 100_000
_REPO = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")


def is_valid_repo_name(full_name: str) -> bool:
    return bool(_REPO.match(full_name)) and ".." not in full_name


def is_doc_path(path: str) -> bool:
    """Documentation worth indexing: Markdown/text files and well-known root files,
    outside vendored, generated and hidden directories."""
    p = PurePosixPath(path)
    if p.is_absolute() or ".." in p.parts or not p.parts:
        return False
    if any(
        part in SKIP_DIRS or (part.startswith(".") and part != ".github") for part in p.parts[:-1]
    ):
        return False
    name = p.name.lower()
    if p.suffix.lower() in DOC_SUFFIXES:
        return True
    return p.suffix == "" and name in DOC_NAMES


def mime_for(path: str) -> str:
    suffix = PurePosixPath(path).suffix.lower()
    return "text/markdown" if suffix in {".md", ".markdown", ".mdx"} else "text/plain"


def file_external_id(path: str) -> str:
    return f"file:{path}"


def issue_external_id(number: int, *, is_pr: bool) -> str:
    return f"{'pr' if is_pr else 'issue'}:{number}"


def checksum(data: bytes | str) -> str:
    raw = data.encode() if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True)
class Repo:
    id: int
    full_name: str
    private: bool
    default_branch: str
    html_url: str
    description: str | None = None


@dataclass(frozen=True, slots=True)
class RepoFile:
    path: str
    data: bytes


@dataclass(frozen=True, slots=True)
class Comment:
    author: str | None
    body: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Issue:
    number: int
    title: str
    body: str
    state: str  # open | closed
    is_pr: bool
    author: str | None
    html_url: str
    created_at: datetime
    updated_at: datetime
    comments: int = 0
    labels: tuple[str, ...] = field(default=())
    merged: bool = False


def issue_markdown(issue: Issue, comments: Sequence[Comment]) -> str:
    """One Markdown document per issue or PR: header, body, then comments. Capped."""
    kind = "Pull request" if issue.is_pr else "Issue"
    status = "merged" if issue.merged else issue.state
    lines = [f"# {kind} #{issue.number}: {issue.title.strip()}", ""]
    meta = [
        f"Status: {status}",
        f"Opened by {issue.author or 'unknown'} on {issue.created_at:%Y-%m-%d}",
    ]
    if issue.labels:
        meta.append("Labels: " + ", ".join(issue.labels))
    lines += [" · ".join(meta), "", issue.body.strip() or "(no description)"]
    for c in list(comments)[:MAX_COMMENTS]:
        if c.body.strip():
            lines += [
                "",
                f"## Comment by {c.author or 'unknown'} on {c.created_at:%Y-%m-%d}",
                "",
                c.body.strip(),
            ]
    text = "\n".join(lines)
    return text if len(text) <= MAX_ISSUE_CHARS else text[:MAX_ISSUE_CHARS] + "\n\n(truncated)"


@dataclass(frozen=True, slots=True)
class SyncCheckpoint:
    """Where the next sync resumes. Stored as the source's ``checkpoint`` JSON."""

    commit: str | None = None  # docs are re-read only when the default branch moves
    issues_since: str | None = None  # ISO timestamp of the newest issue/PR update indexed

    def as_json(self) -> dict[str, object]:
        return {"commit": self.commit, "issues_since": self.issues_since}

    @classmethod
    def from_json(cls, raw: object) -> SyncCheckpoint:
        if not isinstance(raw, dict):
            return cls()
        commit, since = raw.get("commit"), raw.get("issues_since")  # type: ignore[union-attr]
        return cls(
            commit=commit if isinstance(commit, str) else None,
            issues_since=since if isinstance(since, str) else None,
        )


INSTALL_STATE_PURPOSE = "github-install"
