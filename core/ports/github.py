"""What Slate needs from GitHub (ADR-0023). The adapter talks to GitHub as a GitHub App.

Connectors only fetch (rule 2): nothing here parses, chunks, embeds or writes.
Errors: NotFound when the installation or repo is gone or no longer granted;
RateLimited (with retry_after_s) on GitHub's limits; UpstreamUnavailable otherwise.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from core.domain.github import Comment, Issue, Recheck, Repo, RepoFile, TreeSkips, UserAccess


class GitHubApi(Protocol):
    @property
    def configured(self) -> bool:
        """False when the GitHub App settings are missing (the feature is then hidden)."""
        ...

    def install_url(self, state: str) -> str:
        """Where to send the user to install the app on their repos."""
        ...

    async def authorize(
        self, code: str, installation_id: int, *, keep_token: bool
    ) -> UserAccess | None:
        """Exchange the OAuth ``code`` from the install redirect for a user token; if this
        user can access ``installation_id``, return the repos *they* can read through it
        (an org install may grant the app more); else None.

        This stops someone claiming another person's installation id (they're sequential),
        and an org member indexing repos the app sees but they can't. With ``keep_token`` the
        token is returned for encrypted storage (sync-time re-checks); otherwise it's revoked.
        """
        ...

    async def recheck(self, secret: str, installation_id: int) -> Recheck:
        """Which repos the token's user can still read through the installation (refreshing
        the token first when it has expired). Never raises for a dead token: repos=None."""
        ...

    async def forget(self, secret: str) -> None:
        """Revoke a stored user token (best effort)."""
        ...

    async def installation_repos(self, installation_id: int) -> list[Repo]:
        """Repos the user granted to the app on this installation."""
        ...

    async def repo(self, installation_id: int, full_name: str) -> Repo: ...

    async def head_commit(self, installation_id: int, full_name: str, branch: str) -> str: ...

    def doc_files(
        self, installation_id: int, full_name: str, ref: str
    ) -> AsyncIterator[RepoFile | TreeSkips]:
        """Documentation files at ``ref`` (one tarball download; caps in core.domain.github),
        then one ``TreeSkips``: what else the tree held and why it isn't indexed."""
        ...

    def issues(
        self, installation_id: int, full_name: str, since: str | None, limit: int
    ) -> AsyncIterator[Issue]:
        """Issues and PRs updated since ``since`` (ISO), oldest update first."""
        ...

    async def comments(
        self, installation_id: int, full_name: str, number: int
    ) -> list[Comment]: ...
