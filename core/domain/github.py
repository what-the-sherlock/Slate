"""GitHub connector rules (ADR-0023). Pure: no I/O.

What Slate indexes from a repo (owner decision): documentation files and issues/PRs with
their comments. Code is skipped in M4: embedding a whole codebase would use a workspace's
monthly AI allowance in one sync.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePosixPath

from core.domain.skips import SkipReason

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


def doc_path_skip(path: str) -> SkipReason | None:
    """Why a file in the tree isn't indexed (None: it is a doc). Code is unsupported by
    design (ADR-0023), vendored and hidden folders are excluded."""
    if is_doc_path(path):
        return None
    p = PurePosixPath(path)
    if any(
        part in SKIP_DIRS or (part.startswith(".") and part != ".github") for part in p.parts[:-1]
    ):
        return SkipReason.EXCLUDED_PATH
    return SkipReason.UNSUPPORTED_TYPE


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
class TreeSkips:
    """Yielded once, last, by ``GitHubApi.doc_files``: what the tree held that isn't
    indexed, ``{"reason[:kind]": count}`` (core.domain.skips)."""

    counts: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class GitHubSourceConfig:
    """A GitHub source's stored ``config``, parsed. None when it's incomplete (e.g. a
    hand-made fixture or a row from an old version), which the sync reports plainly
    instead of crashing with a KeyError."""

    installation_id: int
    full_name: str

    @classmethod
    def parse(cls, raw: Mapping[str, object]) -> GitHubSourceConfig | None:
        inst, name = raw.get("installation_id"), raw.get("full_name")
        if isinstance(inst, bool) or not isinstance(inst, int | str):
            return None
        try:
            installation = int(inst)
        except ValueError:
            return None
        if installation <= 0 or not isinstance(name, str) or not is_valid_repo_name(name):
            return None
        return cls(installation, name)


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
CREDENTIAL_KIND = "github_user_token"


@dataclass(frozen=True, slots=True)
class UserAccess:
    """What the install redirect proved: the repos *this user* can read through the
    installation, and (when kept) the user's token, serialised for encrypted storage."""

    repos: tuple[str, ...]
    secret: str | None = None


@dataclass(frozen=True, slots=True)
class Recheck:
    """A sync-time re-check (M7 audit): ``repos`` is None when access couldn't be verified
    (token revoked or expired); ``secret`` is the token to store from now on (it may have
    been refreshed, and GitHub rotates refresh tokens on every use)."""

    repos: tuple[str, ...] | None
    secret: str
