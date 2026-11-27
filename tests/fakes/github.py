from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from core.domain.errors import NotFound, RateLimited
from core.domain.github import (
    Comment,
    Issue,
    Recheck,
    Repo,
    RepoFile,
    TreeSkips,
    UserAccess,
    doc_path_skip,
)
from core.domain.skips import SkipReason, Skips


@dataclass
class FakeRepo:
    repo: Repo
    head: str = "c1"
    files: dict[str, bytes] = field(default_factory=dict[str, bytes])
    issues: list[Issue] = field(default_factory=list[Issue])
    comments: dict[int, list[Comment]] = field(default_factory=dict[int, list[Comment]])


class FakeGitHub:
    """An in-memory GitHub App: installations own repos; users own installations."""

    def __init__(self, *, configured: bool = True) -> None:
        self._configured = configured
        self.installations: dict[int, dict[str, FakeRepo]] = {}
        self.owners: dict[str, set[int]] = {}  # OAuth code -> installation ids that user can see
        # OAuth code -> repos that user may read (default: everything on the installation)
        self.user_repos: dict[str, set[str]] = {}
        self.tarball_downloads = 0
        self.comment_calls = 0
        self.rate_limited = False
        self.revoked: set[str] = set()
        self.rechecks = 0
        self.crash_on_file: str | None = None

    @property
    def configured(self) -> bool:
        return self._configured

    def add_repo(self, installation: int, repo: FakeRepo) -> FakeRepo:
        self.installations.setdefault(installation, {})[repo.repo.full_name] = repo
        return repo

    def _get(self, installation_id: int, full_name: str) -> FakeRepo:
        if self.rate_limited:
            raise RateLimited("rate limited", retry_after_s=120)
        repos = self.installations.get(installation_id)
        if repos is None:
            raise NotFound("installation gone")
        if full_name not in repos:
            raise NotFound("repo gone")
        return repos[full_name]

    def install_url(self, state: str) -> str:
        return f"https://github.com/apps/slate/installations/new?state={state}"

    async def authorize(
        self, code: str, installation_id: int, *, keep_token: bool
    ) -> UserAccess | None:
        names = self._names(code, installation_id)
        if names is None:
            return None
        return UserAccess(tuple(names), f"tok:{code}" if keep_token else None)

    def _names(self, code: str, installation_id: int) -> list[str] | None:
        if installation_id not in self.owners.get(code, set()):
            return None
        names = set(self.installations.get(installation_id, {}))
        if code in self.user_repos:
            names &= self.user_repos[code]
        return sorted(names)

    async def recheck(self, secret: str, installation_id: int) -> Recheck:
        self.rechecks += 1
        code = secret.removeprefix("tok:")
        if secret in self.revoked:
            return Recheck(None, secret)
        names = self._names(code, installation_id)
        return Recheck(tuple(names) if names is not None else (), secret)

    async def forget(self, secret: str) -> None:
        self.revoked.add(secret)

    async def installation_repos(self, installation_id: int) -> list[Repo]:
        if installation_id not in self.installations:
            raise NotFound("installation gone")
        return sorted(
            (r.repo for r in self.installations[installation_id].values()),
            key=lambda r: r.full_name,
        )

    async def repo(self, installation_id: int, full_name: str) -> Repo:
        return self._get(installation_id, full_name).repo

    async def head_commit(self, installation_id: int, full_name: str, branch: str) -> str:
        return self._get(installation_id, full_name).head

    async def doc_files(
        self, installation_id: int, full_name: str, ref: str
    ) -> AsyncIterator[RepoFile | TreeSkips]:
        repo = self._get(installation_id, full_name)
        self.tarball_downloads += 1
        skips = Skips()
        for path, data in sorted(repo.files.items()):
            if (reason := doc_path_skip(path)) is not None:
                skips.add(reason, "code" if reason is SkipReason.UNSUPPORTED_TYPE else None)
                continue
            if self.crash_on_file == path:
                raise KeyError("simulated bug")  # an unexpected crash mid-sync (P0-1)
            yield RepoFile(path, data)
        yield TreeSkips(skips.as_json())

    async def issues(
        self, installation_id: int, full_name: str, since: str | None, limit: int
    ) -> AsyncIterator[Issue]:
        repo = self._get(installation_id, full_name)
        ordered = sorted(repo.issues, key=lambda i: i.updated_at)
        n = 0
        for issue in ordered:
            if since and issue.updated_at.isoformat() <= since:
                continue
            yield issue
            n += 1
            if n >= limit:
                return

    async def comments(self, installation_id: int, full_name: str, number: int) -> list[Comment]:
        self.comment_calls += 1
        return list(self._get(installation_id, full_name).comments.get(number, []))
