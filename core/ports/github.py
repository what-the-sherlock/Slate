"""What Slate needs from GitHub (ADR-0023). The adapter talks to GitHub as a GitHub App.

Connectors only fetch (rule 2): nothing here parses, chunks, embeds or writes.
Errors: NotFound when the installation or repo is gone or no longer granted;
RateLimited (with retry_after_s) on GitHub's limits; UpstreamUnavailable otherwise.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from core.domain.github import Comment, Issue, Repo, RepoFile


class GitHubApi(Protocol):
    @property
    def configured(self) -> bool:
        """False when the GitHub App settings are missing (the feature is then hidden)."""
        ...

    def install_url(self, state: str) -> str:
        """Where to send the user to install the app on their repos."""
        ...

    async def user_installation_repos(self, code: str, installation_id: int) -> list[str] | None:
        """Exchange the OAuth ``code`` from the install redirect for a user token; if this
        user can access ``installation_id``, return the repos *they* can read through it
        (an org install may grant the app more); else None. The token is revoked.

        This stops someone claiming another person's installation id (they're sequential),
        and an org member indexing repos the app sees but they can't.
        """
        ...

    async def installation_repos(self, installation_id: int) -> list[Repo]:
        """Repos the user granted to the app on this installation."""
        ...

    async def repo(self, installation_id: int, full_name: str) -> Repo: ...

    async def head_commit(self, installation_id: int, full_name: str, branch: str) -> str: ...

    def doc_files(self, installation_id: int, full_name: str, ref: str) -> AsyncIterator[RepoFile]:
        """Documentation files at ``ref`` (one tarball download; caps in core.domain.github)."""
        ...

    def issues(
        self, installation_id: int, full_name: str, since: str | None, limit: int
    ) -> AsyncIterator[Issue]:
        """Issues and PRs updated since ``since`` (ISO), oldest update first."""
        ...

    async def comments(
        self, installation_id: int, full_name: str, number: int
    ) -> list[Comment]: ...
