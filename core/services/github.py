"""GitHub sources (ADR-0023): connect via the GitHub App, pick repos, sync docs + issues/PRs.

Connect flow (no secret is ever stored):
1. ``install_link`` signs a 15-minute ``state`` bound to this user and workspace.
2. GitHub redirects back with ``installation_id``, an OAuth ``code`` and the ``state``.
   ``complete_install`` checks the state, then asks GitHub (with the code) whether this
   user can really access that installation. Installation ids are sequential, so without
   this anyone could claim someone else's and index their private repos.
3. It returns a signed 30-minute *grant* naming the installation; ``repos`` and
   ``connect`` accept only that grant, so the repo picker needs no extra table.

Sync (``GitHubSync``, a worker/inline job handler): docs are re-read only when the default
branch moved (one tarball); files are skipped when their checksum is unchanged, and
files gone from the repo are pruned. Issues/PRs since the checkpoint are indexed one
document each. Everything goes through IngestService.index_document, so spend caps,
idempotency and ACLs are the same as for uploads (rules 2 and 3). A deadline makes inline
syncs stop cleanly and continue in the background.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import cast
from uuid import UUID

import structlog

from core.domain.auth import Identity, Role
from core.domain.data_class import DataClass
from core.domain.documents import DocumentUpsert
from core.domain.errors import (
    AppError,
    BudgetExceeded,
    Conflict,
    ConnectorError,
    NotFound,
    PermissionDenied,
    RateLimited,
    UpstreamUnavailable,
    ValidationFailed,
)
from core.domain.github import (
    CREDENTIAL_KIND,
    INSTALL_STATE_PURPOSE,
    MAX_ISSUES_PER_SYNC,
    GitHubSourceConfig,
    Repo,
    SyncCheckpoint,
    TreeSkips,
    checksum,
    file_external_id,
    is_valid_repo_name,
    issue_external_id,
    issue_markdown,
    mime_for,
)
from core.domain.ids import JobId, RunId, SourceId
from core.domain.jobs import Job, JobStatus
from core.domain.principals import user_principal
from core.domain.signed_tokens import sign, verify
from core.domain.skips import SkipReason
from core.ports.credentials import CredentialStore
from core.ports.github import GitHubApi
from core.ports.jobs import JobQueue
from core.ports.rate_limit import Limit, RateLimiter
from core.ports.sources import SourceRecord, SourceStore
from core.ports.system import Clock, Parser
from core.services.ingest import IngestService, Stage
from core.services.sync_run import RunBoundary

log = structlog.get_logger(__name__)

SYNC_GITHUB = "sync_github"
CONNECTOR = "github"
GRANT_PURPOSE = "github-grant"
STATE_TTL = timedelta(minutes=15)
GRANT_TTL = timedelta(minutes=30)
MAX_REPOS_PER_CONNECT = 20
PROGRESS_EVERY = 10  # items between live progress updates on a running run
INSTALL_LIMIT = Limit(budget=10, window=timedelta(hours=1))
SYNC_NOW_LIMIT = Limit(budget=1, window=timedelta(minutes=5))
INLINE_SYNC_BUDGET_S = 90.0  # inside the inline runner's 120 s
BAD_CONFIG = "This repository's settings are incomplete. Disconnect it and connect it again."
EXPIRED_LINK = "This GitHub link has expired. Connect GitHub again."
REVERIFY = (
    "Slate couldn't confirm you still have access to this repository. Connect GitHub "
    "again to re-verify; until then it isn't synced."
)


@dataclass(frozen=True, slots=True)
class RepoChoice:
    repo: Repo
    connected: bool


@dataclass(frozen=True, slots=True)
class Connected:
    source_id: SourceId
    job_id: JobId
    name: str


class GitHubService:
    def __init__(
        self,
        gh: GitHubApi,
        sources: SourceStore,
        jobs: JobQueue,
        limiter: RateLimiter,
        clock: Clock,
        *,
        secret: bytes,
        credentials: CredentialStore | None = None,
    ) -> None:
        self._gh = gh
        self._sources = sources
        self._jobs = jobs
        self._limiter = limiter
        self._clock = clock
        self._secret = secret
        # With an encryption key, the user's token is kept so every sync can re-check that
        # they still have access (M7 audit). Without one, access is checked at connect only.
        self._creds = credentials if credentials is not None and credentials.available else None

    @property
    def available(self) -> bool:
        return self._gh.configured

    def _require_admin(self, identity: Identity) -> None:
        if not self.available:
            raise UpstreamUnavailable("GitHub isn't set up on this server yet.")
        if not identity.can(Role.ADMIN):
            raise PermissionDenied("Only admins and the owner can connect GitHub.")

    def install_link(self, identity: Identity) -> str:
        self._require_admin(identity)
        state = sign(
            {"u": str(identity.user_id), "w": str(identity.workspace_id)},
            secret=self._secret,
            purpose=INSTALL_STATE_PURPOSE,
            expires_at=self._clock.now() + STATE_TTL,
        )
        return self._gh.install_url(state)

    async def complete_install(
        self, identity: Identity, *, installation_id: int, code: str, state: str
    ) -> str:
        """Verify the redirect and return a short-lived grant for this installation."""
        self._require_admin(identity)
        self._claims(state, INSTALL_STATE_PURPOSE, identity)
        if not await self._limiter.try_spend(f"gh-install:{identity.user_id}", 1, INSTALL_LIMIT):
            raise RateLimited("Too many GitHub connections. Try again later.", retry_after_s=3600)
        access = (
            await self._gh.authorize(code, installation_id, keep_token=self._creds is not None)
            if installation_id > 0
            else None
        )
        if access is None:
            raise PermissionDenied(
                "GitHub didn't confirm this installation is yours. Connect GitHub again."
            )
        claims: dict[str, object] = {
            "u": str(identity.user_id),
            "w": str(identity.workspace_id),
            "i": installation_id,
            "r": sorted(access.repos),
        }
        if self._creds is not None and access.secret is not None:
            # Stored now, attached to the repos at connect; swept within a day if never used.
            cred = await self._creds.save(
                identity.workspace_id,
                kind=CREDENTIAL_KIND,
                secret=access.secret,
                created_by=identity.user_id,
            )
            claims["k"] = str(cred)
        return sign(
            claims,
            secret=self._secret,
            purpose=GRANT_PURPOSE,
            expires_at=self._clock.now() + GRANT_TTL,
        )

    async def repos(self, identity: Identity, grant: str) -> list[RepoChoice]:
        installation, allowed = self._installation(grant, identity)
        connected = {
            r.name
            for r in await self._sources.sources_by_connector(identity.workspace_id, CONNECTOR)
        }
        repos = await self._gh.installation_repos(installation)
        return [RepoChoice(r, r.full_name in connected) for r in repos if r.full_name in allowed]

    async def connect(
        self, identity: Identity, grant: str, full_names: Sequence[str]
    ) -> list[Connected]:
        installation, allowed = self._installation(grant, identity)
        cred = self._credential(grant, identity)
        wanted = list(dict.fromkeys(full_names))
        if not wanted:
            raise ValidationFailed("Choose at least one repository.")
        if len(wanted) > MAX_REPOS_PER_CONNECT:
            raise ValidationFailed(f"Connect at most {MAX_REPOS_PER_CONNECT} repositories at once.")
        if not all(is_valid_repo_name(n) for n in wanted):
            raise ValidationFailed("That isn't a valid repository name.")
        granted = {
            r.full_name: r
            for r in await self._gh.installation_repos(installation)
            if r.full_name in allowed
        }
        missing = [n for n in wanted if n not in granted]
        if missing:
            raise PermissionDenied(
                "Slate's GitHub App can't see " + ", ".join(missing) + ". Grant it access first."
            )
        out: list[Connected] = []
        ws = identity.workspace_id
        for name in wanted:
            repo = granted[name]
            # Every connected repo is readable only by the person who connected it, and is
            # private data (ADR-0026), even a public repo: anyone can sign up and connect
            # one, so it could carry spam or injected text into others' answers, and a
            # public repo made private later must not stay public. Sharing with workspace
            # members will be an explicit action once invites exist.
            try:
                sid = await self._sources.create_source(
                    ws,
                    connector=CONNECTOR,
                    name=repo.full_name,
                    config={
                        "installation_id": installation,
                        "repo_id": repo.id,
                        "full_name": repo.full_name,
                        "html_url": repo.html_url,
                        "private": repo.private,
                    },
                    visibility=[user_principal(identity.user_id)],
                    data_class=DataClass.PRIVATE,
                    created_by=identity.user_id,
                    credential_id=cred,
                )
            except Conflict:
                # Already connected. Reconnecting refreshes the stored token (that's how a
                # paused "re-verify" repo comes back), but only for your own source.
                await self._reattach(identity, repo.full_name, cred)
                continue
            job = await self._jobs.enqueue(
                ws, SYNC_GITHUB, {"source_id": str(sid)}, f"{SYNC_GITHUB}:{sid}:initial"
            )
            out.append(Connected(sid, job, repo.full_name))
        log.info("github.connected", repos=len(out))
        return out

    async def sync_now(self, identity: Identity, source_id: SourceId) -> JobId:
        if not identity.can(Role.MEMBER):
            raise PermissionDenied("Your role can't sync sources.")
        source = await self._source(identity, source_id)
        # A just-connected repo's first sync is still waiting: run that one, don't add another.
        initial = await self._jobs.find_by_key(f"{SYNC_GITHUB}:{source.id}:initial")
        if initial is not None and initial.status in (JobStatus.QUEUED, JobStatus.FAILED):
            return initial.id
        if not await self._limiter.try_spend(f"gh-sync:{source.id}", 1, SYNC_NOW_LIMIT):
            raise RateLimited(
                "This repository was synced a moment ago. Try again in a few minutes.",
                retry_after_s=300,
            )
        bucket = int(self._clock.now().timestamp()) // 300
        return await self._jobs.enqueue(
            identity.workspace_id,
            SYNC_GITHUB,
            {"source_id": str(source.id)},
            f"{SYNC_GITHUB}:{source.id}:manual:{bucket}",
        )

    async def disconnect(self, identity: Identity, source_id: SourceId) -> None:
        source = await self._source(identity, source_id)
        # Whoever connected it can always delete their own data, whatever their role.
        if not identity.can(Role.ADMIN) and source.created_by != identity.user_id:
            raise PermissionDenied("Only admins, the owner and its connector can disconnect it.")
        if not await self._sources.delete_source(identity.workspace_id, source.id):
            raise NotFound("This source doesn't exist.")
        log.info("github.disconnected")

    async def enqueue_due(self, limit: int = 50) -> int:
        """Hourly worker pass: one sync job per due repo per hour (idempotent key)."""
        hour = int(self._clock.now().timestamp()) // 3600
        due = await self._sources.due_sources(CONNECTOR, limit)
        for ws, sid in due:
            await self._jobs.enqueue(
                ws, SYNC_GITHUB, {"source_id": str(sid)}, f"{SYNC_GITHUB}:{sid}:{hour}"
            )
        return len(due)

    # ------------------------------------------------------------ helpers

    async def _source(self, identity: Identity, source_id: SourceId) -> SourceRecord:
        source = await self._sources.get_source(identity.workspace_id, source_id)
        if source is None or source.connector != CONNECTOR:
            raise NotFound("This source doesn't exist.")
        return source

    def _claims(self, token: str, purpose: str, identity: Identity) -> dict[str, object]:
        try:
            claims = verify(token, secret=self._secret, purpose=purpose, now=self._clock.now())
        except ValidationFailed:
            raise ValidationFailed(EXPIRED_LINK) from None
        if claims.get("u") != str(identity.user_id) or claims.get("w") != str(
            identity.workspace_id
        ):
            raise PermissionDenied(
                "This GitHub link was started by another account. Connect GitHub again."
            )
        return claims

    def _credential(self, grant: str, identity: Identity) -> UUID | None:
        raw = self._claims(grant, GRANT_PURPOSE, identity).get("k")
        try:
            return UUID(str(raw)) if raw is not None else None
        except ValueError:
            return None

    async def _reattach(self, identity: Identity, name: str, cred: UUID | None) -> None:
        if cred is None:
            return
        for s in await self._sources.sources_by_connector(identity.workspace_id, CONNECTOR):
            if s.name == name and s.created_by == identity.user_id:
                await self._sources.set_credential(identity.workspace_id, s.id, cred)

    def _installation(self, grant: str, identity: Identity) -> tuple[int, set[str]]:
        if not self.available:
            raise UpstreamUnavailable("GitHub isn't set up on this server yet.")
        claims = self._claims(grant, GRANT_PURPOSE, identity)
        installation, repos = claims.get("i"), claims.get("r")
        if not isinstance(installation, int) or not isinstance(repos, list):
            raise ValidationFailed(EXPIRED_LINK)
        names = cast(list[object], repos)
        return installation, {str(r) for r in names}


@dataclass
class _Tally:
    seen: int = 0
    changed: int = 0
    failed: int = 0
    pruned: int = 0


class GitHubSync:
    """The ``sync_github`` job handler (worker and inline)."""

    def __init__(
        self,
        gh: GitHubApi,
        sources: SourceStore,
        jobs: JobQueue,
        ingest: IngestService,
        parser: Parser,
        credentials: CredentialStore | None = None,
    ) -> None:
        self._gh = gh
        self._sources = sources
        self._jobs = jobs
        self._ingest = ingest
        self._parser = parser
        self._creds = credentials

    async def handle(self, job: Job) -> None:
        await self.sync(job, deadline=None)

    async def handle_inline(self, job: Job) -> None:
        await self.sync(job, deadline=time.monotonic() + INLINE_SYNC_BUDGET_S)

    async def sync(self, job: Job, *, deadline: float | None) -> None:
        raw = job.payload.get("source_id")
        source = (
            await self._sources.get_source(job.workspace_id, SourceId(_uuid(raw)))
            if isinstance(raw, str)
            else None
        )
        if source is None or source.connector != CONNECTOR or source.status != "active":
            log.info("github.sync_skipped", reason="source gone or paused")
            return
        cp = SyncCheckpoint.from_json(source.checkpoint)
        run = await self._sources.create_run(
            source.id, "full" if cp.commit is None else "incremental"
        )
        tally = _Tally()
        out_of_time = (lambda: False) if deadline is None else (lambda: time.monotonic() > deadline)
        async with RunBoundary(
            self._sources, source.id, run, event="github.synced",
            counts=lambda: tally, checkpoint=lambda: cp.as_json(),
            rate_limited_summary="GitHub's rate limit was reached; syncing resumes later.",
            log_fields=lambda: {"pruned": tally.pruned},
        ) as boundary:  # fmt: skip
            config = GitHubSourceConfig.parse(source.config)
            if config is None:
                raise ConnectorError(BAD_CONFIG, retryable=False, connector=CONNECTOR)
            installation, name = config.installation_id, config.full_name
            await self._recheck(source, installation, name, tally)
            repo = await self._gh.repo(installation, name)
            done = not out_of_time()
            if done:
                cp, done = await self._docs(
                    source, boundary, repo, installation, cp, tally, out_of_time
                )
            if done:
                cp, done = await self._issues(
                    source, run, repo, installation, cp, tally, out_of_time
                )
            if not done:
                boundary.partial("Still syncing: the rest continues in the background.")
                await self._jobs.enqueue(
                    source.workspace_id,
                    SYNC_GITHUB,
                    {"source_id": str(source.id)},
                    f"{SYNC_GITHUB}:{source.id}:cont:{run}",
                )

    async def _recheck(
        self, source: SourceRecord, installation: int, name: str, tally: _Tally
    ) -> None:
        """Does the person who connected this repo still have access (M7 audit)? Can't tell
        (token revoked or expired): stop, keep the documents, ask them to reconnect. No:
        remove everything indexed from it, then stop. Repos connected before tokens were
        kept have no credential and are only checked at connect."""
        if self._creds is None or source.credential_id is None:
            return
        secret = await self._creds.secret(source.workspace_id, source.credential_id)
        if secret is None:
            raise PermissionDenied(REVERIFY)
        result = await self._gh.recheck(secret, installation)
        if result.repos is None:
            # Refresh tokens are single use: a concurrent sync (inline + worker) may have
            # rotated it a moment ago. Its successor is stored by then; try that once.
            latest = await self._creds.secret(source.workspace_id, source.credential_id)
            if latest is not None and latest != secret:
                secret, result = latest, await self._gh.recheck(latest, installation)
        if result.secret != secret:
            await self._creds.update(source.workspace_id, source.credential_id, result.secret)
        if result.repos is None:
            raise PermissionDenied(REVERIFY)
        if name not in result.repos:
            existing = await self._sources.document_checksums(source.id, "")
            tally.pruned = await self._sources.delete_documents(source.id, list(existing))
            raise PermissionDenied(
                "You no longer have access to this repository on GitHub, so what Slate had "
                "indexed from it was removed."
            )

    async def _docs(
        self,
        source: SourceRecord,
        boundary: RunBoundary,
        repo: Repo,
        installation: int,
        cp: SyncCheckpoint,
        tally: _Tally,
        out_of_time: Callable[[], bool],
    ) -> tuple[SyncCheckpoint, bool]:
        head = await self._gh.head_commit(installation, repo.full_name, repo.default_branch)
        if out_of_time():
            return cp, False
        if head == cp.commit:
            return cp, True  # nothing pushed: no download, no spend
        existing = await self._sources.document_checksums(source.id, "file:")
        present: set[str] = set()
        failed_before = tally.failed
        run = boundary.run
        async for f in self._gh.doc_files(installation, repo.full_name, head):
            if isinstance(f, TreeSkips):
                boundary.skips.merge(f.counts)
                continue
            if out_of_time():
                return cp, False
            ext = file_external_id(f.path)
            present.add(ext)  # an empty doc is still present: don't prune it
            if not f.data.strip():
                boundary.skips.add(SkipReason.EMPTY_TEXT, "file")
                continue
            tally.seen += 1
            digest = checksum(f.data)
            if existing.get(ext) == digest:
                continue
            await self._index(
                source, run, tally, ext, f.data, mime_for(f.path),
                title=f.path, uri=f"{repo.html_url}/blob/{repo.default_branch}/{f.path}",
                checksum_=digest, metadata={"repo": repo.full_name, "kind": "file", "path": f.path},
            )  # fmt: skip
        gone = [e for e in existing if e not in present]
        tally.pruned = await self._sources.delete_documents(source.id, gone)
        if tally.failed > failed_before:
            # Keep the old commit so the next sync reads the tree again and retries the
            # files that failed; the ones that worked are skipped by checksum, at no cost.
            return cp, True
        return replace(cp, commit=head), True

    async def _issues(
        self,
        source: SourceRecord,
        run: RunId,
        repo: Repo,
        installation: int,
        cp: SyncCheckpoint,
        tally: _Tally,
        out_of_time: Callable[[], bool],
    ) -> tuple[SyncCheckpoint, bool]:
        # The checkpoint stops at the first issue that failed to index, so the next sync
        # fetches it (and everything after it) again. Unchanged ones re-index for free
        # (checksum), so only the failed ones cost anything.
        stuck = False
        async for issue in self._gh.issues(
            installation, repo.full_name, cp.issues_since, MAX_ISSUES_PER_SYNC
        ):
            if out_of_time():
                return cp, False
            comments = (
                await self._gh.comments(installation, repo.full_name, issue.number)
                if issue.comments
                else []
            )
            text = issue_markdown(issue, comments)
            tally.seen += 1
            kind = "pr" if issue.is_pr else "issue"
            failed_before = tally.failed
            await self._index(
                source,
                run,
                tally,
                issue_external_id(issue.number, is_pr=issue.is_pr),
                text.encode(),
                "text/markdown",
                title=f"#{issue.number} {issue.title.strip()}",
                uri=issue.html_url,
                checksum_=checksum(text),
                metadata={
                    "repo": repo.full_name,
                    "kind": kind,
                    "number": issue.number,
                    "state": "merged" if issue.merged else issue.state,
                },
                author=issue.author,
                created=issue.created_at,
                updated=issue.updated_at,
            )
            stuck = stuck or tally.failed > failed_before
            if not stuck:
                cp = replace(cp, issues_since=issue.updated_at.isoformat())
        return cp, True

    async def _index(
        self,
        source: SourceRecord,
        run: RunId,
        tally: _Tally,
        external_id: str,
        data: bytes,
        mime: str,
        *,
        title: str,
        uri: str,
        checksum_: str,
        metadata: dict[str, object],
        author: str | None = None,
        created: datetime | None = None,
        updated: datetime | None = None,
    ) -> None:
        # The stage the failure happened in: ingestion_errors.stage only allows the real
        # pipeline stages (a CHECK), so a made-up one would crash the run instead.
        stage = Stage("parse")
        if tally.seen % PROGRESS_EVERY == 0:
            await self._sources.progress(
                run, seen=tally.seen, changed=tally.changed, failed=tally.failed
            )
        try:
            parsed = self._parser.parse(data, mime)
            result = await self._ingest.index_document(
                source.workspace_id,
                DocumentUpsert(
                    source_id=source.id,
                    external_id=external_id,
                    uri=uri,
                    title=title,
                    mime=mime,
                    checksum=checksum_,
                    acl=source.visibility,
                    author=author,
                    source_created_at=created,
                    source_updated_at=updated,
                    metadata=metadata,
                ),
                parsed.sections,
                data_class=source.data_class,
                stage=stage,
            )
            if result.changed:
                tally.changed += 1
        except BudgetExceeded:
            raise
        except AppError as e:
            tally.failed += 1
            await self._sources.record_error(
                run,
                external_id=external_id,
                stage=stage.name,
                message=e.detail,
                retryable=e.retryable,
            )


def _uuid(raw: str) -> UUID:
    try:
        return UUID(raw)
    except ValueError:
        return UUID(int=0)  # matches no source: the job is skipped
