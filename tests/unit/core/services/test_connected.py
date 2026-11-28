"""The connected-source sync engine (ADR-0033), with a scripted connector and fakes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pytest

from core.adapters.parsers.formats import FormatParser
from core.domain.auth import Identity, Role
from core.domain.budget import SpendLimits
from core.domain.connected import FetchedDocument, Page
from core.domain.documents import SearchFilters, Section
from core.domain.errors import (
    NotFound,
    PermissionDenied,
    RateLimited,
    ReconnectRequired,
    ValidationFailed,
)
from core.domain.ids import SessionId, SourceId, UserId, WorkspaceId
from core.domain.integrations import REGISTRY, ConnectionStatus, TokenSet, provider
from core.domain.jobs import Job
from core.domain.principals import Principals, user_principal
from core.ports.integrations import ClientCredentials, Connection
from core.services.connected import SYNC_CONNECTED, ConnectedSources
from core.services.ingest import IngestService
from core.services.integrations import IntegrationService
from tests.fakes.integrations import FakeOAuth, InMemoryConnections
from tests.fakes.jobs import FakeJobQueue
from tests.fakes.models import FakeEmbedder, WordTokenizer
from tests.fakes.rate_limit import InMemoryRateLimiter
from tests.fakes.sources import InMemorySources
from tests.fakes.storage import InMemoryBlobStore, InMemoryIndex
from tests.fakes.system import FakeClock
from tests.fakes.usage import InMemoryGenerations, InMemoryLedger

WS = WorkspaceId(uuid4())
ADA = UserId(uuid4())
BOB = UserId(uuid4())
NOW = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)
REG = tuple(replace(p, available=True) if p.id == "notion" else p for p in REGISTRY)


def who(user: UserId = ADA, role: Role = Role.MEMBER) -> Identity:
    return Identity(
        user_id=user, workspace_id=WS, session_id=SessionId(uuid4()), email="a@b.c", name="A",
        role=role, email_verified=True, principals=Principals.for_user(user, WS), csrf_secret="c",
    )  # fmt: skip


def doc(n: int, body: str = "hello") -> FetchedDocument:
    return FetchedDocument(
        external_id=f"notion:{n}", title=f"Page {n}", uri=f"https://notion.so/{n}",
        sections=(Section(f"Page {n}", f"{body} {n}"),), author="Ada",
        updated_at=NOW - timedelta(days=n),
    )  # fmt: skip


class Script:
    """A connector that returns scripted pages (or raises) and records what it was given."""

    def __init__(self, *pages: Page | Exception) -> None:
        self.pages = list(pages)
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def pull(self, token: str, checkpoint: Mapping[str, object], *, now: datetime) -> Page:
        self.calls.append((token, dict(checkpoint)))
        nxt = self.pages.pop(0) if self.pages else Page(checkpoint=dict(checkpoint))
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


class Env:
    def __init__(
        self, connector: Script, *, spent: Decimal = Decimal(0), parser: FormatParser | None = None
    ) -> None:
        self.clock = FakeClock(NOW)
        self.index = InMemoryIndex()
        self.sources = InMemorySources(self.index)
        self.jobs = FakeJobQueue(self.clock)
        self.store = InMemoryConnections(now=NOW)
        self.oauth = FakeOAuth(tokens=TokenSet("tok-1", "ref-1", NOW + timedelta(hours=1)))
        self.limiter = InMemoryRateLimiter(self.clock)
        self.integrations = IntegrationService(
            self.store, self.oauth, self.sources, self.limiter, self.clock, secret=b"k" * 32,
            clients={"notion": ClientCredentials("id", "secret")},
            redirect_uri="https://slate.test/integrations/callback", legacy_ready={},
            registry=REG,
        )  # fmt: skip
        ingest = IngestService(
            InMemoryBlobStore(), self.index, self.sources, FormatParser(), WordTokenizer(),
            FakeEmbedder(dims=16), InMemoryGenerations(), InMemoryLedger(spent),
            limits=SpendLimits.monthly_only(Decimal(5)),
        )  # fmt: skip
        self.connector = connector
        self.engine = ConnectedSources(
            self.sources, self.jobs, ingest, self.store, self.integrations, self.limiter,
            self.clock, connectors={"notion": connector}, specs={p.id: p for p in REG},
            parser=parser,
        )  # fmt: skip
        for p in self.engine.providers:
            self.integrations.on_connected(p, self.engine.connected)

    async def connect(self, user: UserId = ADA) -> Connection:
        url = await self.integrations.start(who(user), "notion")
        state = parse_qs(urlparse(url).query)["state"][0]
        return await self.integrations.complete(who(user), "notion", "code", state)

    def source(self) -> SourceId:
        [sid] = [s for s, r in self.sources.records.items() if r.connector == "notion"]
        return sid

    def queued(self) -> list[Job]:
        return [j for j in self.jobs.jobs.values() if j.kind == SYNC_CONNECTED]

    async def run(self, *, inline: bool = False) -> None:
        job = self.queued()[-1]
        if inline:
            await self.engine.handle_inline(job)
        else:
            await self.engine.handle(job)

    def last_run(self) -> tuple[str, int, int, int, str | None]:
        return list(self.sources.finished.values())[-1]


# ---------------------------------------------------------------- connect


async def test_connecting_creates_a_private_source_and_queues_its_first_sync() -> None:
    env = Env(Script())
    conn = await env.connect()
    source = env.sources.records[env.source()]
    assert source.connection_id == conn.id
    assert source.visibility == (user_principal(ADA),)
    assert source.created_by == ADA
    assert source.name == "Notion · ada@example.test"
    assert len(env.queued()) == 1


async def test_reconnecting_reuses_the_source_and_queues_another_sync() -> None:
    env = Env(Script())
    await env.connect()
    env.clock._now = NOW + timedelta(minutes=5)
    await env.connect()
    assert len([r for r in env.sources.records.values() if r.connector == "notion"]) == 1
    assert len(env.queued()) == 2


async def test_another_members_same_account_gets_its_own_uniquely_named_source() -> None:
    env = Env(Script())
    await env.connect(ADA)
    await env.connect(BOB)
    names = sorted(r.name for r in env.sources.records.values() if r.connector == "notion")
    assert names[0] == "Notion · ada@example.test"
    assert names[1].startswith("Notion · ada@example.test (")


# ---------------------------------------------------------------- sync


async def test_a_sync_indexes_every_page_and_saves_the_final_checkpoint() -> None:
    env = Env(Script(
        Page((doc(1), doc(2)), checkpoint={"cursor": "c2"}, done=False),
        Page((doc(3),), checkpoint={"since": "x"}, done=True),
    ))  # fmt: skip
    await env.connect()
    await env.run()
    assert env.last_run()[:3] == ("succeeded", 3, 3)
    assert env.sources.records[env.source()].checkpoint == {"since": "x"}
    assert [c[1] for c in env.connector.calls] == [{}, {"cursor": "c2"}]
    assert all(c[0] == "tok-1" for c in env.connector.calls)


async def test_unchanged_documents_cost_nothing_on_the_next_sync() -> None:
    env = Env(Script(Page((doc(1),)), Page((doc(1),))))
    await env.connect()
    await env.run()
    env.jobs.jobs.clear()
    await env.engine.sync_connection(who(), (await env.store.list_for_user(WS, ADA))[0].id)
    await env.run()
    assert env.last_run()[:3] == ("succeeded", 1, 0)  # seen, not re-indexed


async def test_deleted_ids_prefixes_and_keep_only_remove_documents() -> None:
    env = Env(Script(
        Page((doc(1), doc(2), doc(3)), checkpoint={"a": 1}),
        Page(deleted=("notion:1",), checkpoint={"b": 1}, done=False),
        Page(deleted_prefixes=("notion:2",), checkpoint={"c": 1}, done=False),
        Page((doc(4),), keep_only=("notion:4",), checkpoint={"d": 1}),
    ))  # fmt: skip
    await env.connect()
    await env.run()
    await env.engine.sync_now(who(), env.source())
    await env.run()
    left = env.index.checksums(env.source(), "notion:")
    assert sorted(left) == ["notion:4"]


async def test_a_run_out_of_time_stops_cleanly_and_continues_in_a_new_job() -> None:
    env = Env(Script(*(Page((doc(n),), checkpoint={"n": n}, done=False) for n in range(500))))
    await env.connect()
    job = env.queued()[-1]
    await env.engine.sync(job, deadline=0.0)  # already past the deadline
    status, *_rest, summary = env.last_run()
    assert status == "partial"
    assert summary is not None
    assert "continues in the background" in summary
    assert len(env.queued()) == 2  # the continuation


async def test_a_dead_grant_fails_the_run_plainly_and_hourly_syncs_skip_it() -> None:
    env = Env(Script(ReconnectRequired("gone", connector="Notion")))
    await env.connect()
    await env.run()
    status, *_rest, summary = env.last_run()
    assert status == "failed"
    assert summary == "Sign in to Notion again to keep this source up to date."
    assert env.sources.errors[-1][3] is False  # not retryable
    conn = (await env.store.list_for_user(WS, ADA))[0]
    await env.store.mark(conn.id, ConnectionStatus.NEEDS_RECONNECT, "revoked")
    env.jobs.jobs.clear()
    env.sources.records[env.source()] = replace(
        env.sources.records[env.source()], last_synced_at=None
    )
    assert await env.engine.enqueue_due() == 0


async def test_a_rate_limit_keeps_the_checkpoint_and_retries_the_job() -> None:
    env = Env(Script(
        Page((doc(1),), checkpoint={"page": 1}, done=False),
        RateLimited("slow down", retry_after_s=30),
    ))  # fmt: skip
    await env.connect()
    with pytest.raises(RateLimited):
        await env.run()
    assert env.last_run()[0] == "partial"
    assert env.sources.records[env.source()].checkpoint == {"page": 1}


async def test_an_exhausted_budget_pauses_the_sync_without_losing_its_place() -> None:
    env = Env(Script(Page((doc(1),), checkpoint={"page": 1})), spent=Decimal(5))
    await env.connect()
    await env.run()
    status, *_rest, summary = env.last_run()
    assert status == "partial"
    assert summary is not None


async def test_an_expired_token_is_refreshed_before_pulling() -> None:
    env = Env(Script(Page((doc(1),))))
    env.oauth.tokens = TokenSet("old", "ref-1", NOW + timedelta(minutes=1))
    env.oauth.refresh_outcome = TokenSet("fresh", "ref-2", NOW + timedelta(hours=1))
    await env.connect()
    await env.run()
    assert env.connector.calls[0][0] == "fresh"


async def test_a_removed_source_or_paused_one_is_skipped() -> None:
    env = Env(Script(Page((doc(1),))))
    await env.connect()
    sid = env.source()
    env.sources.records[sid] = replace(env.sources.records[sid], status="paused")
    await env.run()
    assert env.connector.calls == []


# ---------------------------------------------------------------- ownership


async def test_only_the_person_who_connected_it_can_sync_or_disconnect() -> None:
    env = Env(Script())
    await env.connect()
    with pytest.raises(NotFound):
        await env.engine.sync_now(who(BOB, Role.ADMIN), env.source())
    with pytest.raises(NotFound):
        await env.engine.disconnect(who(BOB, Role.ADMIN), env.source())
    with pytest.raises(PermissionDenied):
        await env.engine.sync_now(who(ADA, Role.VIEWER), env.source())


async def test_sync_now_is_rate_limited_per_source() -> None:
    env = Env(Script())
    await env.connect()
    await env.engine.sync_now(who(), env.source())
    with pytest.raises(RateLimited):
        await env.engine.sync_now(who(), env.source())


async def test_disconnecting_from_the_source_revokes_and_deletes_the_connection() -> None:
    env = Env(Script())
    await env.connect()
    await env.engine.disconnect(who(), env.source())
    assert env.oauth.revoked
    assert await env.store.list_for_user(WS, ADA) == []


async def test_sync_connection_repairs_a_setup_whose_source_is_missing() -> None:
    env = Env(Script())
    conn = await env.connect()
    env.sources.records.pop(env.source())  # e.g. the hook failed after sign-in
    sid = await env.engine.sync_connection(who(), conn.id)
    assert env.sources.records[sid].connection_id == conn.id
    with pytest.raises(NotFound):
        await env.engine.sync_connection(who(BOB), conn.id)


# ---------------------------------------------------------------- the grant


async def test_a_sign_in_missing_a_required_scope_is_refused_and_revoked() -> None:
    env = Env(Script())
    gmail = provider("gmail")
    env.integrations._registry = (replace(gmail, available=True),)
    env.integrations._by_id = {"gmail": env.integrations._registry[0]}
    env.integrations._clients = {"google": ClientCredentials("g", "s")}
    env.oauth.tokens = TokenSet("a", "r", None, ("openid", "email"))  # gmail.readonly unticked
    url = await env.integrations.start(who(), "gmail")
    state = parse_qs(urlparse(url).query)["state"][0]
    with pytest.raises(ValidationFailed, match="every permission"):
        await env.integrations.complete(who(), "gmail", "code", state)
    assert env.oauth.revoked == ["a"]
    assert await env.store.list_for_user(WS, ADA) == []


async def test_a_google_sign_in_without_a_refresh_token_is_refused() -> None:
    env = Env(Script())
    gmail = replace(provider("gmail"), available=True)
    env.integrations._registry = (gmail,)
    env.integrations._by_id = {"gmail": gmail}
    env.integrations._clients = {"google": ClientCredentials("g", "s")}
    scope = "https://www.googleapis.com/auth/gmail.readonly"
    env.oauth.tokens = TokenSet("a", None, None, ("openid", scope))
    url = await env.integrations.start(who(), "gmail")
    state = parse_qs(urlparse(url).query)["state"][0]
    with pytest.raises(ValidationFailed, match="lasting access"):
        await env.integrations.complete(who(), "gmail", "code", state)


# ---------------------------------------------------------------- honest outcomes (P0-1, P0-4)


async def test_an_unexpected_crash_is_failed_and_re_raised() -> None:
    env = Env(Script(KeyError("page body text")))
    await env.connect()
    with pytest.raises(KeyError):
        await env.run()
    status, *_, summary = env.last_run()
    assert status == "failed"
    assert "KeyError" in (summary or "")
    assert "page body" not in (summary or "")
    assert env.source() not in env.sources.synced


async def test_empty_documents_and_page_skips_are_counted() -> None:
    empty = FetchedDocument(external_id="notion:9", title="Blank", uri="https://notion.so/9",
                            sections=(), metadata={"kind": "notion"})  # fmt: skip
    env = Env(Script(Page((doc(1), empty), skipped={"unsupported_type:pdf": 2})))
    await env.connect()
    await env.run()
    status, seen, *_ = env.last_run()
    assert (status, seen) == ("succeeded", 1)  # the empty one isn't "seen"
    [skipped] = list(env.sources.skipped.values())
    assert skipped == {"empty_text:notion": 1, "unsupported_type:pdf": 2}


def raw(n: int, data: bytes, mime: str) -> FetchedDocument:
    return FetchedDocument(
        external_id=f"notion:{n}",
        title=f"File {n}",
        uri=f"https://x/{n}",
        sections=(),
        mime=mime,
        raw=data,
        metadata={"kind": "drive"},
    )


async def test_raw_files_are_parsed_by_the_engine_and_bad_ones_counted() -> None:
    """P1-1: a connector hands over bytes; the engine parses them with the uploads' parser.
    A file the parser can't read, or a type it doesn't know, is a counted skip, not a crash."""
    csv = raw(1, b"item,cost\nlaptops,9000\n", "text/csv")
    broken = raw(2, b"%PDF-1.7 not really", "application/pdf")
    odd = raw(3, b"\x00\x01", "application/x-unknown")
    env = Env(Script(Page((csv, broken, odd))), parser=FormatParser())
    await env.connect()
    await env.run()
    status, seen, *_ = env.last_run()
    assert (status, seen) == ("succeeded", 1)
    [skipped] = list(env.sources.skipped.values())
    assert skipped == {"unreadable:drive": 1, "unsupported_type:drive": 1}
    [chunk] = [c.chunk for c in env.index._chunks]
    assert "laptops" in chunk.content
    assert "9000" in chunk.content
    assert (chunk.heading_path or "").startswith("File 1")


async def test_without_a_parser_raw_files_are_unsupported_not_dropped() -> None:
    env = Env(Script(Page((raw(1, b"a,b\n1,2\n", "text/csv"),))))
    await env.connect()
    await env.run()
    [skipped] = list(env.sources.skipped.values())
    assert skipped == {"unsupported_type:drive": 1}


# ---------------------------------------------------------------- attachments (ADR-0043)


def mail_doc(files: dict[str, bytes]) -> FetchedDocument:
    from core.domain.attachments import AttachmentBlob, AttachmentInfo

    infos = [
        AttachmentInfo(key=f"seed-{i}", name=name, mime="text/plain", size=len(data))
        for i, (name, data) in enumerate(files.items())
    ]
    return FetchedDocument(
        external_id="thread:t1", title="Test", uri="https://mail.test/t1",
        sections=(Section("Email > Test", "From me to Aashutosh: (no text)"),),
        author="Me", updated_at=NOW,
        metadata={"kind": "email", "attachments": [i.to_meta() for i in infos]},
        attachments=tuple(AttachmentBlob(i, d) for i, d in zip(infos, files.values(), strict=True)),
    )  # fmt: skip


def with_attachments(env: Env) -> Env:
    from core.services.attachments import AttachmentIndexer

    ingest = env.engine._ingest
    assert ingest is not None
    env.engine._attachments = AttachmentIndexer(ingest, env.index, env.sources, FormatParser())
    return env


async def test_a_synced_conversations_files_become_searchable_children() -> None:
    env = with_attachments(Env(Script(Page((mail_doc({"plan.txt": b"Launch in March"}),)))))
    await env.connect()
    await env.run()
    ada, bob = Principals.for_user(ADA, WS), Principals.for_user(BOB, WS)
    hits = await env.index.keyword_search(WS, ada, "march", 5, SearchFilters())
    assert hits
    assert hits[0].parent_id is not None
    parents = [
        h for h in await env.index.keyword_search(WS, ada, "test", 5, SearchFilters())
        if h.parent_id is None
    ]  # fmt: skip
    assert [a.state.value for a in parents[0].attachments] == ["text"]
    # Bob shares nothing with Ada's private mail, attachments included.
    assert await env.index.keyword_search(WS, bob, "march", 5, SearchFilters()) == []


async def test_a_file_removed_from_the_conversation_is_pruned_at_the_next_sync() -> None:
    env = with_attachments(Env(Script(
        Page((mail_doc({"a.txt": b"alpha", "b.txt": b"bravo"}),), checkpoint={"c": 1}),
        Page((mail_doc({"a.txt": b"alpha"}),), checkpoint={"c": 2}),
    )))  # fmt: skip
    await env.connect()
    await env.run()
    await env.run()
    ada = Principals.for_user(ADA, WS)
    assert await env.index.keyword_search(WS, ada, "bravo", 5, SearchFilters()) == []
    assert await env.index.keyword_search(WS, ada, "alpha", 5, SearchFilters())
