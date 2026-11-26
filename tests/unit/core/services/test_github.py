from __future__ import annotations

import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from core.adapters.parsers.formats import FormatParser
from core.domain.auth import Identity, Role
from core.domain.budget import SpendLimits
from core.domain.documents import ParsedDocument, SearchFilters
from core.domain.errors import NotFound, PermissionDenied, RateLimited, ValidationFailed
from core.domain.github import Comment, Issue, Repo
from core.domain.ids import JobId, SessionId, SourceId, UserId, WorkspaceId
from core.domain.jobs import Job, JobStatus
from core.domain.principals import PUBLIC, Principals, user_principal, workspace_principal
from core.services.github import SYNC_GITHUB, GitHubService, GitHubSync
from core.services.ingest import IngestService
from tests.fakes.github import FakeGitHub, FakeRepo
from tests.fakes.jobs import FakeJobQueue
from tests.fakes.models import FakeEmbedder, WordTokenizer
from tests.fakes.rate_limit import InMemoryRateLimiter
from tests.fakes.sources import InMemorySources
from tests.fakes.storage import InMemoryBlobStore, InMemoryIndex
from tests.fakes.system import FakeClock
from tests.fakes.usage import InMemoryGenerations, InMemoryLedger

INSTALL = 42
T0 = datetime(2026, 11, 1, tzinfo=UTC)


def identity(
    role: Role = Role.OWNER, ws: WorkspaceId | None = None, user: UserId | None = None
) -> Identity:
    u, w = user or UserId(uuid4()), ws or WorkspaceId(uuid4())
    return Identity(
        user_id=u, workspace_id=w, session_id=SessionId(uuid4()), email="a@b.c", name="A",
        role=role, principals=Principals.for_user(u, w), csrf_secret="c", email_verified=True,
    )  # fmt: skip


def issue(n: int, updated: datetime, *, pr: bool = False, comments: int = 0) -> Issue:
    return Issue(
        n,
        f"Issue {n}",
        f"Body of {n}",
        "open",
        pr,
        "ada",
        f"https://gh/o/r/issues/{n}",
        T0,
        updated,
        comments,
    )


class Env:
    def __init__(self, *, spent: Decimal = Decimal(0)) -> None:
        self.clock = FakeClock()
        self.gh = FakeGitHub()
        self.index = InMemoryIndex()
        self.sources = InMemorySources(self.index)
        self.jobs = FakeJobQueue(self.clock)
        self.ledger = InMemoryLedger(spent)
        self.svc = GitHubService(
            self.gh,
            self.sources,
            self.jobs,
            InMemoryRateLimiter(self.clock),
            self.clock,
            secret=b"s" * 32,
        )
        ingest = IngestService(
            InMemoryBlobStore(),
            self.index,
            self.sources,
            FormatParser(),
            WordTokenizer(),
            FakeEmbedder(dims=16),
            InMemoryGenerations(),
            self.ledger,
            limits=SpendLimits.monthly_only(Decimal(5)),
        )
        self.sync = GitHubSync(self.gh, self.sources, self.jobs, ingest, FormatParser())
        self.me = identity()
        self.repo = self.gh.add_repo(
            INSTALL,
            FakeRepo(
                Repo(1, "octo/docs", True, "main", "https://github.com/octo/docs"),
                files={
                    "README.md": b"# Docs\n\nInstall with pip.",
                    "docs/faq.md": b"# FAQ\n\nAsk away.",
                },
                issues=[
                    issue(1, T0 + timedelta(days=1), comments=1),
                    issue(2, T0 + timedelta(days=2), pr=True),
                ],
                comments={1: [Comment("bob", "Fixed in v2.", T0)]},
            ),
        )
        self.gh.owners["good-code"] = {INSTALL}

    async def grant(self, who: Identity | None = None) -> str:
        who = who or self.me
        url = self.svc.install_link(who)
        state = url.split("state=", 1)[1]
        return await self.svc.complete_install(
            who, installation_id=INSTALL, code="good-code", state=state
        )

    async def connect(self, *names: str) -> SourceId:
        out = await self.svc.connect(self.me, await self.grant(), list(names) or ["octo/docs"])
        return out[0].source_id

    async def run(self, sid: SourceId, *, deadline: float | None = None) -> None:
        job = Job(
            JobId(uuid4()),
            self.me.workspace_id,
            SYNC_GITHUB,
            {"source_id": str(sid)},
            JobStatus.RUNNING,
            1,
            5,
            T0,
            "k",
        )
        await self.sync.sync(job, deadline=deadline)

    def docs(self, sid: SourceId) -> set[str]:
        return set(self.index.checksums(sid, ""))

    def last_run(self) -> tuple[str, int, int, int, str | None]:
        return list(self.sources.finished.values())[-1]


@pytest.fixture
def env() -> Env:
    return Env()


# ---------------------------------------------------------------- connect flow


async def test_install_link_needs_an_admin(env: Env) -> None:
    with pytest.raises(PermissionDenied):
        env.svc.install_link(identity(Role.MEMBER))
    assert "state=" in env.svc.install_link(env.me)


async def test_the_installation_must_belong_to_the_user(env: Env) -> None:
    state = env.svc.install_link(env.me).split("state=", 1)[1]
    with pytest.raises(PermissionDenied, match="didn't confirm"):
        await env.svc.complete_install(
            env.me, installation_id=INSTALL, code="someone-elses-code", state=state
        )
    with pytest.raises(PermissionDenied):
        await env.svc.complete_install(env.me, installation_id=99, code="good-code", state=state)


async def test_a_state_from_another_account_or_an_old_link_is_refused(env: Env) -> None:
    state = env.svc.install_link(env.me).split("state=", 1)[1]
    with pytest.raises(PermissionDenied, match="another account"):
        await env.svc.complete_install(
            identity(), installation_id=INSTALL, code="good-code", state=state
        )
    env.clock.advance(timedelta(minutes=16))
    with pytest.raises(ValidationFailed, match="expired"):
        await env.svc.complete_install(
            env.me, installation_id=INSTALL, code="good-code", state=state
        )
    with pytest.raises(ValidationFailed):
        await env.svc.complete_install(
            env.me, installation_id=INSTALL, code="good-code", state="forged"
        )


async def test_a_grant_works_only_for_its_account(env: Env) -> None:
    grant = await env.grant()
    with pytest.raises(PermissionDenied):
        await env.svc.repos(identity(), grant)


async def test_repos_are_listed_with_connected_flags(env: Env) -> None:
    grant = await env.grant()
    assert [(c.repo.full_name, c.connected) for c in await env.svc.repos(env.me, grant)] == [
        ("octo/docs", False)
    ]
    await env.svc.connect(env.me, grant, ["octo/docs"])
    assert (await env.svc.repos(env.me, grant))[0].connected


async def test_connect_creates_a_source_only_the_connector_can_read(
    env: Env,
) -> None:
    sid = await env.connect()
    rec = env.sources.records[sid]
    assert rec.name == "octo/docs"
    assert rec.data_class == "private"
    # ADR-0026: private to whoever connected it, not the whole workspace, so a future
    # invite never shares a repo nobody chose to share.
    assert rec.visibility == (user_principal(env.me.user_id),)
    assert workspace_principal(env.me.workspace_id) not in rec.visibility
    assert rec.created_by == env.me.user_id
    assert rec.config["installation_id"] == INSTALL
    (job,) = env.jobs.jobs.values()
    assert job.kind == SYNC_GITHUB
    assert job.idempotency_key.endswith(":initial")


async def test_public_repos_stay_inside_the_workspace(env: Env) -> None:
    env.gh.add_repo(
        INSTALL, FakeRepo(Repo(2, "octo/open", False, "main", "https://github.com/octo/open"))
    )
    sid = await env.connect("octo/open")
    rec = env.sources.records[sid]
    # Security review: a public repo connected by any account must not reach other accounts
    # (spam, injected text), and must not stay public if it's made private later.
    assert rec.data_class == "private"
    assert PUBLIC not in rec.visibility


async def test_org_members_only_see_repos_they_can_read(env: Env) -> None:
    env.gh.add_repo(INSTALL, FakeRepo(Repo(3, "octo/secret", True, "main", "u")))
    env.gh.user_repos["good-code"] = {"octo/docs"}  # the app sees more than this user
    grant = await env.grant()
    assert [c.repo.full_name for c in await env.svc.repos(env.me, grant)] == ["octo/docs"]
    with pytest.raises(PermissionDenied):
        await env.svc.connect(env.me, grant, ["octo/secret"])


async def test_connect_refuses_repos_the_app_cant_see_and_too_many(env: Env) -> None:
    grant = await env.grant()
    with pytest.raises(PermissionDenied, match="can't see"):
        await env.svc.connect(env.me, grant, ["someone/secret"])
    with pytest.raises(ValidationFailed):
        await env.svc.connect(env.me, grant, [f"o/r{i}" for i in range(21)])
    with pytest.raises(ValidationFailed):
        await env.svc.connect(env.me, grant, [])


async def test_connecting_twice_is_a_no_op(env: Env) -> None:
    await env.connect()
    assert await env.svc.connect(env.me, await env.grant(), ["octo/docs"]) == []
    assert len(env.sources.records) == 1


async def test_unconfigured_server_says_so(env: Env) -> None:
    env.gh._configured = False
    with pytest.raises(Exception, match="isn't set up"):
        env.svc.install_link(env.me)


# ---------------------------------------------------------------- sync


async def test_first_sync_indexes_docs_issues_and_prs(env: Env) -> None:
    sid = await env.connect()
    await env.run(sid)
    assert env.docs(sid) == {"file:README.md", "file:docs/faq.md", "issue:1", "pr:2"}
    status, seen, changed, failed, _ = env.last_run()
    assert (status, seen, changed, failed) == ("succeeded", 4, 4, 0)
    cp = env.sources.records[sid].checkpoint
    assert cp == {"commit": "c1", "issues_since": (T0 + timedelta(days=2)).isoformat()}
    assert sid in env.sources.synced


async def test_issue_documents_include_comments(env: Env) -> None:
    sid = await env.connect()
    await env.run(sid)
    hits = await env.index.keyword_search(
        env.me.workspace_id,
        env.me.principals,
        "Fixed",
        10,
        SearchFilters(),
    )
    assert any("Fixed in v2" in h.content for h in hits)


async def test_an_unchanged_repo_costs_nothing_on_the_next_sync(env: Env) -> None:
    sid = await env.connect()
    await env.run(sid)
    spent = len(env.ledger.entries)
    await env.run(sid)
    assert env.gh.tarball_downloads == 1  # head commit unchanged: no download
    assert len(env.ledger.entries) == spent  # no embeddings
    assert env.last_run()[:3] == ("succeeded", 0, 0)


async def test_a_push_reindexes_only_changed_files_and_prunes_deleted_ones(env: Env) -> None:
    sid = await env.connect()
    await env.run(sid)
    env.repo.head = "c2"
    env.repo.files = {
        "README.md": b"# Docs\n\nInstall with uv now.",
        "docs/new.md": b"# New\n\nHello.",
    }
    await env.run(sid)
    assert env.docs(sid) == {"file:README.md", "file:docs/new.md", "issue:1", "pr:2"}  # faq pruned
    _, seen, changed, _, _ = env.last_run()
    assert (seen, changed) == (2, 2)


async def test_only_issues_updated_since_the_checkpoint_are_fetched(env: Env) -> None:
    sid = await env.connect()
    await env.run(sid)
    env.repo.issues.append(issue(3, T0 + timedelta(days=3)))
    calls = env.gh.comment_calls
    await env.run(sid)
    assert "issue:3" in env.docs(sid)
    assert env.last_run()[1] == 1  # only #3 seen
    assert env.gh.comment_calls == calls  # #3 has no comments: none fetched


async def test_a_deadline_stops_cleanly_and_queues_the_rest(env: Env) -> None:
    sid = await env.connect()
    await env.run(sid, deadline=time.monotonic() - 1)  # already out of time
    status, _, _, _, summary = env.last_run()
    assert status == "partial"
    assert summary
    assert "background" in summary
    assert any(":cont:" in j.idempotency_key for j in env.jobs.jobs.values())
    assert sid not in env.sources.synced  # not a full pass
    await env.run(sid)  # the continuation finishes the job
    assert env.docs(sid) == {"file:README.md", "file:docs/faq.md", "issue:1", "pr:2"}


async def test_a_repo_the_app_lost_access_to_fails_with_a_clear_reason(env: Env) -> None:
    sid = await env.connect()
    env.gh.installations[INSTALL].clear()
    await env.run(sid)
    status, *_, summary = env.last_run()
    assert status == "failed"
    assert summary
    assert "repo gone" in summary


async def test_rate_limits_save_progress_and_retry_later(env: Env) -> None:
    sid = await env.connect()
    env.gh.rate_limited = True
    with pytest.raises(RateLimited):
        await env.run(sid)
    assert env.last_run()[0] == "partial"


async def test_a_spent_budget_stops_the_sync_without_failing_it(env: Env) -> None:
    env.ledger.spent = Decimal(5)
    sid = await env.connect()
    await env.run(sid)
    status, *_, summary = env.last_run()
    assert status == "partial"
    assert summary
    assert "budget" in summary.lower()


async def test_a_job_for_a_deleted_source_is_skipped(env: Env) -> None:
    await env.run(SourceId(uuid4()))
    assert env.sources.finished == {}


# ---------------------------------------------------------------- sync now, disconnect, schedule


async def test_sync_now_runs_the_waiting_first_sync_then_is_rate_limited(env: Env) -> None:
    sid = await env.connect()
    (initial,) = env.jobs.jobs.values()
    assert await env.svc.sync_now(env.me, sid) == initial.id  # no duplicate job
    env.jobs.jobs[initial.id] = replace(initial, status=JobStatus.SUCCEEDED)
    manual = await env.svc.sync_now(env.me, sid)
    assert manual != initial.id
    with pytest.raises(RateLimited):
        await env.svc.sync_now(env.me, sid)
    with pytest.raises(NotFound):
        await env.svc.sync_now(identity(), sid)  # another workspace


async def test_disconnect_deletes_everything_indexed(env: Env) -> None:
    sid = await env.connect()
    await env.run(sid)
    with pytest.raises(PermissionDenied):
        await env.svc.disconnect(identity(Role.MEMBER, ws=env.me.workspace_id), sid)
    await env.svc.disconnect(env.me, sid)
    assert sid not in env.sources.records
    assert env.docs(sid) == set()


async def test_due_repos_get_one_job_per_hour(env: Env) -> None:
    await env.connect()
    assert await env.svc.enqueue_due() == 1
    before = len(env.jobs.jobs)
    await env.svc.enqueue_due()
    assert len(env.jobs.jobs) == before  # same hour, same idempotency key


def checkpoint(env: Env, sid: SourceId) -> dict[str, object]:
    return env.sources.records[sid].checkpoint or {}


class _FlakyParser:
    """Fails to parse any text containing BROKEN, as a corrupt file would."""

    def __init__(self) -> None:
        self._real = FormatParser()
        self.broken = True

    def supports(self, mime: str) -> bool:
        return self._real.supports(mime)

    def parse(self, data: bytes, mime: str) -> ParsedDocument:
        if self.broken and b"BROKEN" in data:
            raise ValidationFailed("This file couldn't be read.")
        return self._real.parse(data, mime)


async def test_a_file_that_failed_is_retried_on_the_next_sync_without_a_push(env: Env) -> None:
    flaky = _FlakyParser()
    env.sync._parser = flaky
    env.repo.files["docs/broken.md"] = b"# Broken\n\nBROKEN content."
    sid = await env.connect()
    await env.run(sid)
    assert "file:docs/broken.md" not in env.docs(sid)
    assert env.last_run()[0] == "partial"
    assert checkpoint(env, sid)["commit"] is None  # not past the failure

    flaky.broken = False  # e.g. a parser fix shipped; no new push to the repo
    await env.run(sid)
    assert "file:docs/broken.md" in env.docs(sid)
    assert env.last_run()[:3] == ("succeeded", 3, 1)  # only the failed file cost anything
    assert checkpoint(env, sid)["commit"] == "c1"


async def test_an_issue_that_failed_holds_the_issue_checkpoint_back(env: Env) -> None:
    flaky = _FlakyParser()
    env.sync._parser = flaky
    env.repo.issues[0] = replace(env.repo.issues[0], body="BROKEN body")
    sid = await env.connect()
    await env.run(sid)
    assert "issue:1" not in env.docs(sid)
    assert "pr:2" in env.docs(sid)  # later items still index this run
    assert checkpoint(env, sid)["issues_since"] is None

    flaky.broken = False
    await env.run(sid)
    assert "issue:1" in env.docs(sid)
    assert checkpoint(env, sid)["issues_since"] == (T0 + timedelta(days=2)).isoformat()


# ------------------------------------------- access re-checked on every sync (M7 audit)


class Rechecking(Env):
    """Env with an encryption key: the user's token is kept and re-checked each sync."""

    def __init__(self) -> None:
        super().__init__()
        from tests.fakes.credentials import InMemoryCredentials

        self.creds = InMemoryCredentials()
        self.svc = GitHubService(
            self.gh, self.sources, self.jobs, InMemoryRateLimiter(self.clock), self.clock,
            secret=b"s" * 32, credentials=self.creds,
        )  # fmt: skip
        self.sync = GitHubSync(
            self.gh, self.sources, self.jobs, self.sync._ingest, FormatParser(),
            credentials=self.creds,
        )  # fmt: skip

    def status(self) -> tuple[str, str | None]:
        st = list(self.sources.finished.values())[-1]
        return st[0], st[4]


async def test_token_is_kept_encrypted_and_attached_to_the_repo() -> None:
    env = Rechecking()
    sid = await env.connect()
    cred = env.sources.records[sid].credential_id
    assert cred is not None
    assert await env.creds.secret(env.me.workspace_id, cred) == "tok:good-code"


async def test_losing_access_removes_the_repo_documents() -> None:
    env = Rechecking()
    sid = await env.connect()
    await env.run(sid)
    assert env.index.checksums(sid, "")
    env.gh.user_repos["good-code"] = set()  # removed from the org / repo
    await env.run(sid)
    assert env.status()[0] == "failed"
    assert "no longer have access" in (env.status()[1] or "")
    assert env.index.checksums(sid, "") == {}


async def test_an_unverifiable_token_pauses_but_keeps_documents() -> None:
    env = Rechecking()
    sid = await env.connect()
    await env.run(sid)
    docs = env.index.checksums(sid, "")
    env.gh.revoked.add("tok:good-code")
    await env.run(sid)
    assert env.status()[0] == "failed"
    assert "re-verify" in (env.status()[1] or "")
    assert env.index.checksums(sid, "") == docs


async def test_reconnecting_reattaches_a_fresh_token() -> None:
    env = Rechecking()
    sid = await env.connect()
    old = env.sources.records[sid].credential_id
    env.gh.owners["good-code-2"] = {INSTALL}
    state = env.svc.install_link(env.me).split("state=", 1)[1]
    grant = await env.svc.complete_install(
        env.me, installation_id=INSTALL, code="good-code-2", state=state
    )
    assert await env.svc.connect(env.me, grant, ["octo/docs"]) == []  # already connected
    new = env.sources.records[sid].credential_id
    assert new is not None
    assert new != old


async def test_repos_without_a_stored_token_still_sync() -> None:
    env = Rechecking()
    sid = await env.connect()
    from dataclasses import replace as _replace

    env.sources.records[sid] = _replace(env.sources.records[sid], credential_id=None)  # legacy
    await env.run(sid)
    assert env.status()[0] == "succeeded"
    assert env.gh.rechecks == 0
