from __future__ import annotations

import io
import tarfile
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from core.adapters.github.app import GitHubApp
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


def _real_app_over(files: dict[str, bytes]) -> GitHubApp:
    """The real GitHubApp adapter, with GitHub itself replaced by MockTransport."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path, data in files.items():
            info = tarfile.TarInfo(f"octo-docs-sha1/{path}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    tarball = buf.getvalue()
    repo = {"id": 1, "full_name": "octo/docs", "private": True, "default_branch": "main",
            "html_url": "https://github.com/octo/docs"}  # fmt: skip
    issues = [{"number": 1, "title": "Bug", "body": "It breaks.", "state": "open",
               "html_url": "https://github.com/octo/docs/issues/1", "comments": 1,
               "created_at": "2026-11-01T00:00:00Z", "updated_at": "2026-11-02T00:00:00Z",
               "user": {"login": "ada"}}]  # fmt: skip
    comments = [{"body": "Fixed in v2.", "created_at": "2026-11-02T00:00:00Z",
                 "user": {"login": "bob"}}]  # fmt: skip

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/access_tokens"):
            return httpx.Response(
                201, json={"token": "ghs_x", "expires_at": "2099-01-01T00:00:00Z"}
            )
        if path == "/repos/octo/docs":
            return httpx.Response(200, json=repo)
        if path == "/repos/octo/docs/commits/main":
            return httpx.Response(200, text="sha1\n")
        if path == "/repos/octo/docs/tarball/sha1":
            return httpx.Response(200, content=tarball)
        if path == "/repos/octo/docs/issues":
            return httpx.Response(200, json=issues)
        if path == "/repos/octo/docs/issues/1/comments":
            return httpx.Response(200, json=comments)
        return httpx.Response(404)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    return GitHubApp(
        app_id="1", slug="slate", client_id="c", client_secret="s", private_key=pem,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )  # fmt: skip


async def test_sync_with_the_real_github_app_indexes_docs_and_issues(env: Env) -> None:
    # The fake GitHubApi hid P0-0 (a TypeError inside the real adapter). This wires the
    # real adapter under the service, so the adapter's own plumbing runs too.
    sid = await env.connect()
    real = _real_app_over({"README.md": b"# Docs\n\nInstall with pip.", "src/app.py": b"code"})
    env.sync = GitHubSync(real, env.sources, env.jobs, env.sync._ingest, FormatParser())
    await env.run(sid)
    status, seen, changed, failed, summary = env.last_run()
    assert (status, seen, changed, failed, summary) == ("succeeded", 2, 2, 0, None)
    assert env.docs(sid) == {"file:README.md", "issue:1"}  # code is never indexed (ADR-0023)
    assert checkpoint(env, sid)["commit"] == "sha1"


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


# ---------------------------------------------------------------- honest outcomes (P0-1, P0-4)

DOC_TEXT = "Q3 salary bands"  # stands in for document text inside an exception message


async def test_a_crash_mid_sync_is_failed_not_an_empty_success(env: Env) -> None:
    sid = await env.connect()
    env.gh.crash_on_file = "docs/faq.md"
    with pytest.raises(KeyError):  # re-raised: the worker retries the job
        await env.run(sid)
    status, _, _, _, summary = env.last_run()
    assert status == "failed"
    assert summary
    assert "KeyError" in summary
    assert sid not in env.sources.synced  # last_synced_at doesn't move
    assert all(DOC_TEXT not in e[2] for e in env.sources.errors)


async def test_an_incomplete_config_is_a_plain_failure_not_a_key_error(env: Env) -> None:
    sid = await env.connect()
    rec = env.sources.records[sid]
    for bad in ({}, {"installation_id": "x", "full_name": "octo/docs"},
                {"installation_id": True, "full_name": "octo/docs"},
                {"installation_id": INSTALL, "full_name": "../etc"}):  # fmt: skip
        env.sources.records[sid] = replace(rec, config=bad)
        await env.run(sid)  # no exception: nothing to retry, the reason is shown
        status, *_, summary = env.last_run()
        assert status == "failed"
        assert summary
        assert "settings are incomplete" in summary


async def test_files_left_out_are_counted_with_a_reason(env: Env) -> None:
    env.repo.files.update({"src/app.py": b"print()", "node_modules/x/README.md": b"v",
                           "docs/blank.md": b"  \n"})  # fmt: skip
    sid = await env.connect()
    await env.run(sid)
    [skipped] = list(env.sources.skipped.values())
    assert skipped == {"empty_text:file": 1, "excluded_path": 1, "unsupported_type:code": 1}
    assert "file:docs/blank.md" not in env.docs(sid)
    assert env.last_run()[0] == "succeeded"
