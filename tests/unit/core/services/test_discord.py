"""DiscordService (connect) and DiscordSync (sync) against an in-memory Discord."""

from __future__ import annotations

import itertools
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from core.adapters.parsers.formats import FormatParser
from core.domain.auth import Identity, Role
from core.domain.budget import SpendLimits
from core.domain.discord import (
    READ,
    VIEW_CHANNEL,
    Channel,
    ChannelType,
    Guild,
    Member,
    Message,
    Overwrite,
    SyncCheckpoint,
    Thread,
    snowflake_at,
)
from core.domain.discord import (
    Role as DRole,
)
from core.domain.documents import SearchFilters
from core.domain.errors import (
    Conflict,
    NotFound,
    PermissionDenied,
    RateLimited,
    UpstreamUnavailable,
    ValidationFailed,
)
from core.domain.ids import JobId, SessionId, SourceId, UserId, WorkspaceId
from core.domain.jobs import Job, JobStatus
from core.domain.principals import Principals, user_principal, workspace_principal
from core.services.discord import SYNC_DISCORD, DiscordService
from core.services.discord_sync import INTENT_OFF, DiscordSync
from core.services.ingest import IngestService
from tests.fakes.discord import BOT_ID, FakeDiscord, FakeServer
from tests.fakes.jobs import FakeJobQueue
from tests.fakes.models import FakeEmbedder, WordTokenizer
from tests.fakes.rate_limit import InMemoryRateLimiter
from tests.fakes.sources import InMemorySources
from tests.fakes.storage import InMemoryBlobStore, InMemoryIndex
from tests.fakes.system import FakeClock
from tests.fakes.usage import InMemoryGenerations, InMemoryLedger

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
G = "200000000000000000"
OWNER = "200000000000000001"
ME = "200000000000000002"  # the connecting user's Discord id
MODS = "200000000000000003"
GENERAL = "200000000000000010"
STAFF = "200000000000000011"  # private: mods only
FORUM = "200000000000000012"
BOTLESS = "200000000000000013"  # bot denied
_ids = itertools.count(1)


def identity(
    role: Role = Role.OWNER, ws: WorkspaceId | None = None, user: UserId | None = None
) -> Identity:
    u, w = user or UserId(uuid4()), ws or WorkspaceId(uuid4())
    return Identity(
        user_id=u, workspace_id=w, session_id=SessionId(uuid4()), email="a@b.c", name="A",
        role=role, principals=Principals.for_user(u, w), csrf_secret="c", email_verified=True,
    )  # fmt: skip


def msg(when: datetime, text: str, channel: str = GENERAL, **kw: object) -> Message:
    sid = str(snowflake_at(when) + next(_ids))
    return Message(id=sid, channel_id=channel, author_id=ME, author_name="ana", content=text, **kw)  # type: ignore[arg-type]


def server(dc: FakeDiscord, *, my_roles: tuple[str, ...] = (MODS,)) -> FakeServer:
    g = Guild(G, "Acme", OWNER, (DRole(G, "@everyone", READ), DRole(MODS, "mods", 0)))
    s = dc.add_server(g)
    hide = Overwrite(G, 0, 0, VIEW_CHANNEL)
    s.channels = {
        GENERAL: Channel(GENERAL, "general", ChannelType.TEXT, position=0),
        STAFF: Channel(
            STAFF, "staff", ChannelType.TEXT, position=1,
            overwrites=(hide, Overwrite(MODS, 0, VIEW_CHANNEL, 0),
                        Overwrite(BOT_ID, 1, VIEW_CHANNEL, 0)),
        ),
        FORUM: Channel(FORUM, "help", ChannelType.FORUM, position=2),
        BOTLESS: Channel(
            BOTLESS, "secret", ChannelType.TEXT, position=3,
            overwrites=(Overwrite(BOT_ID, 1, 0, VIEW_CHANNEL),),
        ),
    }  # fmt: skip
    s.members[ME] = Member(ME, my_roles)
    return s


class Env:
    def __init__(self, *, spent: Decimal = Decimal(0)) -> None:
        self.clock = FakeClock(NOW)
        self.dc = FakeDiscord()
        self.srv = server(self.dc)
        self.index = InMemoryIndex()
        self.sources = InMemorySources(self.index)
        self.jobs = FakeJobQueue(self.clock)
        self.ledger = InMemoryLedger(spent)
        self.embedder = FakeEmbedder(dims=16)
        self.svc = DiscordService(
            self.dc, self.sources, self.jobs, InMemoryRateLimiter(self.clock), self.clock,
            secret=b"s" * 32,
        )  # fmt: skip
        self.ingest = IngestService(
            InMemoryBlobStore(), self.index, self.sources, FormatParser(), WordTokenizer(),
            self.embedder, InMemoryGenerations(), self.ledger,
            limits=SpendLimits.monthly_only(Decimal(5)),
        )  # fmt: skip
        self.sync = DiscordSync(self.dc, self.sources, self.jobs, self.ingest, self.clock)
        self.me = identity()
        self._codes = itertools.count(1)

    async def grant(self, who: Identity | None = None, *, user: str = ME) -> str:
        who = who or self.me
        state = self.svc.install_link(who).split("state=", 1)[1]
        code = f"code{next(self._codes):08d}"
        self.dc.codes[code] = (G, user)
        return await self.svc.complete_install(who, guild_id=G, code=code, state=state)

    async def connect(self, *channels: str, who: Identity | None = None) -> SourceId:
        done = await self.svc.connect(who or self.me, await self.grant(who), list(channels))
        return done.source_id

    def job(self, sid: SourceId, ws: WorkspaceId | None = None) -> Job:
        return Job(
            id=JobId(uuid4()), workspace_id=ws or self.me.workspace_id, kind=SYNC_DISCORD,
            payload={"source_id": str(sid)}, status=JobStatus.RUNNING, attempts=1,
            max_attempts=5, run_after=NOW, idempotency_key="k",
        )  # fmt: skip

    async def run(self, sid: SourceId, *, deadline: float | None = None) -> str:
        await self.sync.sync(self.job(sid), deadline=deadline)
        return self.last_status()

    def last_status(self) -> str:
        return list(self.sources.finished.values())[-1][0]

    def docs(self, sid: SourceId, prefix: str = "") -> dict[str, str]:
        return self.index.checksums(sid, prefix)

    async def find(self, word: str, who: Identity | None = None) -> list[str]:
        who = who or self.me
        hits = await self.index.keyword_search(
            who.workspace_id, who.principals, word, 20, SearchFilters()
        )
        return [h.content for h in hits]


@pytest.fixture
def env() -> Env:
    return Env()


# ================================================================== connect


async def test_install_needs_admin_and_configuration(env: Env) -> None:
    with pytest.raises(PermissionDenied):
        env.svc.install_link(identity(Role.MEMBER))
    off = DiscordService(
        FakeDiscord(configured=False), env.sources, env.jobs,
        InMemoryRateLimiter(env.clock), env.clock, secret=b"s" * 32,
    )  # fmt: skip
    with pytest.raises(UpstreamUnavailable):
        off.install_link(env.me)


async def test_grant_lists_only_channels_the_user_can_read(env: Env) -> None:
    server_ = await env.svc.channels(env.me, await env.grant())
    rows = {r.channel.id: r for r in server_.rows}
    assert rows[STAFF].private
    assert rows[STAFF].selectable
    assert not rows[BOTLESS].selectable
    assert not rows[BOTLESS].bot_can_read
    env.srv.members[ME] = Member(ME, ())  # not a mod any more
    rows = {r.channel.id: r for r in (await env.svc.channels(env.me, await env.grant())).rows}
    assert not rows[STAFF].user_can_read
    assert not rows[STAFF].selectable


async def test_cannot_connect_a_channel_you_cannot_read(env: Env) -> None:
    env.srv.members[ME] = Member(ME, ())
    with pytest.raises(PermissionDenied, match="read yourself"):
        await env.connect(STAFF)


async def test_cannot_connect_a_channel_the_bot_cannot_read(env: Env) -> None:
    with pytest.raises(PermissionDenied, match="bot can't read"):
        await env.connect(BOTLESS)


@pytest.mark.parametrize(
    "bad", ["not-a-code", "wrong-guild", "reused"], ids=["unknown", "other-guild", "reused"]
)
async def test_install_must_be_proven(env: Env, bad: str) -> None:
    state = env.svc.install_link(env.me).split("state=", 1)[1]
    code = "codeAAAAAAAA"
    if bad == "wrong-guild":
        env.dc.codes[code] = ("200000000000000999", ME)
    elif bad == "reused":
        env.dc.codes[code] = (G, ME)
        await env.svc.complete_install(env.me, guild_id=G, code=code, state=state)
    with pytest.raises(PermissionDenied):
        await env.svc.complete_install(env.me, guild_id=G, code=code, state=state)


async def test_install_state_is_bound_to_user_and_workspace(env: Env) -> None:
    state = env.svc.install_link(env.me).split("state=", 1)[1]
    env.dc.codes["codeBBBBBBBB"] = (G, ME)
    with pytest.raises(PermissionDenied, match="another account"):
        await env.svc.complete_install(identity(), guild_id=G, code="codeBBBBBBBB", state=state)
    with pytest.raises(ValidationFailed, match="expired"):
        await env.svc.complete_install(env.me, guild_id=G, code="codeBBBBBBBB", state="x.y")


async def test_grant_cannot_be_used_by_another_account(env: Env) -> None:
    grant = await env.grant()
    with pytest.raises(PermissionDenied):
        await env.svc.connect(identity(ws=env.me.workspace_id), grant, [GENERAL])
    env.clock.advance(timedelta(minutes=31))
    with pytest.raises(ValidationFailed, match="expired"):
        await env.svc.connect(env.me, grant, [GENERAL])


async def test_connect_makes_a_private_connector_only_source(env: Env) -> None:
    sid = await env.connect(GENERAL, STAFF)
    rec = env.sources.records[sid]
    assert rec.visibility == (user_principal(env.me.user_id),)
    assert workspace_principal(env.me.workspace_id) not in rec.visibility
    assert rec.data_class == "private"
    assert rec.created_by == env.me.user_id
    assert rec.config["guild_id"] == G
    (job,) = env.jobs.jobs.values()
    assert job.idempotency_key == f"{SYNC_DISCORD}:{sid}:initial"


async def test_connecting_more_channels_extends_the_same_source(env: Env) -> None:
    sid = await env.connect(GENERAL)
    again = await env.svc.connect(env.me, await env.grant(), [STAFF, GENERAL])
    assert again.source_id == sid
    assert again.channels == 2
    assert len(env.sources.records) == 1
    rows = (await env.svc.channels(env.me, await env.grant())).rows
    assert {r.channel.id for r in rows if r.connected} == {GENERAL, STAFF}


async def test_another_member_cannot_add_channels_to_my_source(env: Env) -> None:
    await env.connect(GENERAL)
    colleague = identity(ws=env.me.workspace_id)
    with pytest.raises(Conflict):
        await env.svc.connect(colleague, await env.grant(colleague), [STAFF])


async def test_two_servers_with_the_same_name_get_distinct_sources(env: Env) -> None:
    await env.connect(GENERAL)
    other = Guild("300000000000000000", "Acme", OWNER, (DRole("300000000000000000", "e", READ),))
    s2 = env.dc.add_server(other)
    s2.channels["300000000000000010"] = Channel("300000000000000010", "x", ChannelType.TEXT)
    s2.members[ME] = Member(ME, ())
    state = env.svc.install_link(env.me).split("state=", 1)[1]
    env.dc.codes["codeCCCCCCCC"] = (other.id, ME)
    grant = await env.svc.complete_install(
        env.me, guild_id=other.id, code="codeCCCCCCCC", state=state
    )
    done = await env.svc.connect(env.me, grant, ["300000000000000010"])
    assert done.name == "Acme (000000)"


async def test_connect_validates_its_input(env: Env) -> None:
    grant = await env.grant()
    with pytest.raises(ValidationFailed):
        await env.svc.connect(env.me, grant, [])
    with pytest.raises(ValidationFailed):
        await env.svc.connect(env.me, grant, ["../etc"])
    with pytest.raises(PermissionDenied):
        await env.svc.connect(env.me, grant, ["299999999999999999"])  # not in the grant


async def test_sync_now_and_disconnect(env: Env) -> None:
    sid = await env.connect(GENERAL)
    initial = await env.svc.sync_now(env.me, sid)
    assert env.jobs.jobs[initial].idempotency_key.endswith(":initial")
    with pytest.raises(PermissionDenied):
        await env.svc.disconnect(identity(Role.MEMBER, ws=env.me.workspace_id), sid)
    with pytest.raises(NotFound):
        await env.svc.disconnect(identity(), sid)  # another workspace: plain 404
    await env.svc.disconnect(env.me, sid)
    assert sid not in env.sources.records
    assert env.dc.left == [G]  # no other source uses the server: the bot leaves


async def test_bot_stays_while_another_account_still_uses_the_server(env: Env) -> None:
    mine = await env.connect(GENERAL)
    other = identity()
    await env.connect(GENERAL, who=other)
    await env.svc.disconnect(env.me, mine)
    assert env.dc.left == []


async def test_manual_sync_is_rate_limited(env: Env) -> None:
    sid = await env.connect(GENERAL)
    env.jobs.jobs.clear()  # the initial job ran
    await env.svc.sync_now(env.me, sid)
    with pytest.raises(RateLimited):
        await env.svc.sync_now(env.me, sid)


# ================================================================== sync


async def connected(env: Env, *channels: str) -> SourceId:
    return await env.connect(*(channels or (GENERAL,)))


async def test_first_sync_indexes_recent_history_only(env: Env) -> None:
    env.dc.post(GENERAL, msg(NOW - timedelta(days=120), "ancient lore"))
    env.dc.post(GENERAL, msg(NOW - timedelta(days=2), "deploy freeze starts friday"))
    sid = await connected(env)
    assert await env.run(sid) == "succeeded"
    assert await env.find("freeze")
    assert not await env.find("ancient")  # older than 90 days
    run_kind = list(env.sources.runs.values())[-1][1]
    assert run_kind == "full"


async def test_synced_content_is_invisible_to_anyone_else(env: Env) -> None:
    env.dc.post(GENERAL, msg(NOW - timedelta(hours=1), "salary bands are secret"))
    sid = await connected(env)
    await env.run(sid)
    assert await env.find("salary")
    colleague = identity(ws=env.me.workspace_id)  # same workspace, not the connector
    assert await env.find("salary", colleague) == []
    outsider = identity()
    assert await env.find("salary", outsider) == []
    assert env.ledger.entries[0].data_class == "private"


async def test_unchanged_server_costs_nothing_on_resync(env: Env) -> None:
    env.dc.post(GENERAL, msg(NOW - timedelta(hours=3), "release notes ready"))
    sid = await connected(env)
    await env.run(sid)
    calls = len(env.embedder.calls)
    assert await env.run(sid) == "succeeded"
    assert len(env.embedder.calls) == calls
    assert list(env.sources.runs.values())[-1][1] == "incremental"


async def test_new_messages_extend_the_open_window(env: Env) -> None:
    first = msg(NOW - timedelta(minutes=20), "standup moved")
    env.dc.post(GENERAL, first)
    sid = await connected(env)
    await env.run(sid)
    env.clock.advance(timedelta(minutes=5))
    env.dc.post(GENERAL, msg(NOW - timedelta(minutes=10), "to ten thirty"))
    await env.run(sid)
    docs = env.docs(sid, "msg:")
    assert list(docs) == [f"msg:{GENERAL}:{first.id}"]  # one conversation, rebuilt
    assert any("thirty" in c for c in await env.find("thirty"))


async def test_recent_edit_and_delete_are_picked_up(env: Env) -> None:
    a = msg(NOW - timedelta(hours=5), "budget is ten")
    b = msg(NOW - timedelta(hours=2), "lunch at noon")  # separate window (>30 min gap)
    env.dc.post(GENERAL, a, b)
    sid = await connected(env)
    await env.run(sid)
    msgs = env.srv.messages[GENERAL]
    msgs[0] = Message(a.id, GENERAL, ME, "ana", "budget is twelve", edited=True)
    msgs.remove(b)
    await env.run(sid)
    assert await env.find("twelve")
    assert not await env.find("ten")
    assert not await env.find("lunch")  # the deleted window is pruned
    assert len(env.docs(sid, "msg:")) == 1


async def test_old_edits_outside_the_reread_period_are_not_fetched(env: Env) -> None:
    a = msg(NOW - timedelta(days=5), "policy v1")
    b = msg(NOW - timedelta(days=4), "unrelated")  # the newest window is always re-read
    env.dc.post(GENERAL, a, b)
    sid = await connected(env)
    await env.run(sid)
    env.srv.messages[GENERAL][0] = Message(a.id, GENERAL, ME, "ana", "policy v2")
    await env.run(sid)
    assert await env.find("v1")  # documented limitation: older than 48 h
    assert len(env.docs(sid, "msg:")) == 2  # and never pruned by mistake


async def test_forum_posts_and_threads_are_indexed_but_private_threads_never(env: Env) -> None:
    post = Thread("200000000000000100", FORUM, "How do I reset my token?", None)
    secret = Thread("200000000000000101", GENERAL, "hr case", None, private=True)
    env.srv.threads = {post.id: post, secret.id: secret}
    env.dc.post(post.id, msg(NOW - timedelta(hours=1), "rotate it in settings", channel=post.id))
    env.dc.post(secret.id, msg(NOW - timedelta(hours=1), "confidential", channel=secret.id))
    sid = await connected(env, GENERAL, FORUM)
    assert await env.run(sid) == "succeeded"
    assert await env.find("rotate")
    assert not await env.find("confidential")
    assert f"thread:{FORUM}:{post.id}" in env.docs(sid, "thread:")


async def test_deleted_thread_is_pruned(env: Env) -> None:
    t = Thread("200000000000000102", FORUM, "Old question", None)
    env.srv.threads[t.id] = t
    env.dc.post(t.id, msg(NOW - timedelta(hours=1), "answer here", channel=t.id))
    sid = await connected(env, FORUM)
    await env.run(sid)
    assert env.docs(sid, "thread:")
    del env.srv.threads[t.id]
    del env.srv.messages[t.id]
    await env.run(sid)
    assert env.docs(sid, "thread:") == {}


async def test_unchanged_threads_are_not_refetched_after_the_reread_period(env: Env) -> None:
    t = Thread("200000000000000103", FORUM, "Old thread", None)
    env.srv.threads[t.id] = t
    env.dc.post(t.id, msg(NOW - timedelta(days=10), "settled", channel=t.id))
    sid = await connected(env, FORUM)
    await env.run(sid)
    env.dc.thread_fetches.clear()
    await env.run(sid)
    assert env.dc.thread_fetches == []  # quiet for 10 days: floor is past it


async def test_deleted_channel_removes_its_documents(env: Env) -> None:
    env.dc.post(GENERAL, msg(NOW - timedelta(hours=1), "goodbye world"))
    sid = await connected(env)
    await env.run(sid)
    del env.srv.channels[GENERAL]
    await env.run(sid)
    assert env.docs(sid) == {}
    assert any("no longer exists" in e[2] for e in env.sources.errors)


async def test_channel_the_bot_lost_is_reported_others_keep_syncing(env: Env) -> None:
    env.dc.post(GENERAL, msg(NOW - timedelta(hours=1), "alpha"))
    env.dc.post(STAFF, msg(NOW - timedelta(hours=1), "bravo"))
    sid = await connected(env, GENERAL, STAFF)
    await env.run(sid)
    staff = env.srv.channels[STAFF]
    env.srv.channels[STAFF] = Channel(
        STAFF, "staff", ChannelType.TEXT, overwrites=(Overwrite(BOT_ID, 1, 0, VIEW_CHANNEL),)
    )
    env.dc.post(GENERAL, msg(NOW - timedelta(minutes=1), "charlie"))
    assert await env.run(sid) == "partial"
    assert await env.find("charlie")
    assert await env.find("bravo")  # kept, like a repo the app can't see
    assert any("can't read #staff" in e[2] for e in env.sources.errors)
    assert staff.id == STAFF


async def test_server_gone_fails_the_run(env: Env) -> None:
    sid = await connected(env)
    env.srv.bot_in = False
    assert await env.run(sid) == "failed"


async def test_missing_message_content_intent_is_explained(env: Env) -> None:
    env.dc.post(GENERAL, *(msg(NOW - timedelta(minutes=i), "") for i in range(30)))
    sid = await connected(env)
    assert await env.run(sid) == "failed"
    assert list(env.sources.finished.values())[-1][4] == INTENT_OFF


async def test_rate_limit_saves_progress_and_retries(env: Env) -> None:
    env.dc.post(GENERAL, msg(NOW - timedelta(hours=1), "first"))
    sid = await connected(env)
    await env.run(sid)
    env.dc.rate_limited = True
    with pytest.raises(RateLimited):
        await env.sync.sync(env.job(sid), deadline=None)
    assert env.last_status() == "partial"
    env.dc.rate_limited = False
    env.dc.post(GENERAL, msg(NOW - timedelta(minutes=1), "second"))
    assert await env.run(sid) == "succeeded"
    assert await env.find("second")


async def test_deadline_stops_cleanly_and_continues_in_the_background(env: Env) -> None:
    env.dc.post(GENERAL, msg(NOW - timedelta(hours=1), "later"))
    sid = await connected(env)
    env.jobs.jobs.clear()
    assert await env.run(sid, deadline=time.monotonic() - 1) == "partial"
    (cont,) = env.jobs.jobs.values()
    assert ":cont:" in cont.idempotency_key
    assert await env.run(sid) == "succeeded"
    assert await env.find("later")


async def test_budget_exhausted_is_partial_and_resumes(env: Env) -> None:
    env.dc.post(GENERAL, msg(NOW - timedelta(hours=1), "costly words"))
    sid = await connected(env)
    env.ledger.spent = Decimal(10)
    assert await env.run(sid) == "partial"
    assert not await env.find("costly")
    env.ledger.spent = Decimal(0)
    assert await env.run(sid) == "succeeded"
    assert await env.find("costly")


async def test_skipped_when_source_is_gone_or_from_another_workspace(env: Env) -> None:
    sid = await connected(env)
    await env.sync.sync(env.job(sid, ws=WorkspaceId(uuid4())), deadline=None)
    assert env.sources.runs == {}
    bogus = replace(env.job(sid), payload={"source_id": "not-a-uuid"})
    await env.sync.sync(bogus, deadline=None)
    assert env.sources.runs == {}


async def test_checkpoint_survives_a_round_trip(env: Env) -> None:
    env.dc.post(GENERAL, msg(NOW - timedelta(hours=1), "persist me"))
    sid = await connected(env)
    await env.run(sid)
    cp = SyncCheckpoint.from_json(env.sources.records[sid].checkpoint)
    assert cp.cursor(GENERAL).last_id is not None
    assert cp.cursor(GENERAL).threads_floor is not None


async def test_enqueue_due_is_idempotent_per_hour(env: Env) -> None:
    await connected(env)
    env.jobs.jobs.clear()
    assert await env.svc.enqueue_due() == 1
    await env.svc.enqueue_due()
    assert len(env.jobs.jobs) == 1


def _fail_once(env: Env, match: str, error: Exception) -> list[str]:
    """Make index_document raise ``error`` the first time it sees an id containing
    ``match``. Returns the ids it was called with."""
    real = env.ingest.index_document
    seen: list[str] = []
    state = {"armed": True}

    async def flaky(ws, doc, sections, **kw):
        seen.append(doc.external_id)
        if state["armed"] and match in doc.external_id:
            state["armed"] = False
            raise error
        return await real(ws, doc, sections, **kw)

    env.ingest.index_document = flaky
    return seen


async def test_a_window_that_fails_is_retried_next_sync(env: Env) -> None:
    a = msg(NOW - timedelta(hours=6), "first topic")
    b = msg(NOW - timedelta(hours=3), "second topic")
    env.dc.post(GENERAL, a, b)
    sid = await connected(env)
    _fail_once(env, a.id, ValidationFailed("No text found in this file."))
    assert await env.run(sid) == "partial"
    assert all(e[1] in {"fetch", "parse", "chunk", "embed", "store"} for e in env.sources.errors)
    assert await env.run(sid) == "succeeded"
    assert await env.find("first")
    assert await env.find("second")


async def test_budget_stop_in_the_thread_loop_never_skips_a_thread(env: Env) -> None:
    from core.domain.errors import BudgetExceeded

    t1 = Thread("200000000000000110", FORUM, "one", None)
    t2 = Thread("200000000000000111", FORUM, "two", None)
    env.srv.threads = {t1.id: t1, t2.id: t2}
    env.dc.post(t1.id, msg(NOW - timedelta(hours=3), "alpha thread", channel=t1.id))
    env.dc.post(t2.id, msg(NOW - timedelta(hours=1), "omega thread", channel=t2.id))
    sid = await connected(env, FORUM)
    _fail_once(env, t2.id, BudgetExceeded("This workspace's AI allowance for today is used up."))
    assert await env.run(sid) == "partial"
    assert await env.find("alpha")
    assert not await env.find("omega")
    assert await env.run(sid) == "succeeded"
    assert await env.find("omega")


async def test_channel_added_later_gets_its_own_backfill(env: Env) -> None:
    env.dc.post(GENERAL, msg(NOW - timedelta(hours=1), "general chatter"))
    env.dc.post(STAFF, msg(NOW - timedelta(days=30), "staff memo"))
    sid = await connected(env, GENERAL)
    await env.run(sid)
    await env.svc.connect(env.me, await env.grant(), [STAFF])
    await env.run(sid)
    assert await env.find("memo")


async def test_a_capped_pass_resumes_instead_of_rereading(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    import core.services.discord_sync as mod

    monkeypatch.setattr(mod, "MAX_MESSAGES_PER_CHANNEL_PER_SYNC", 3)
    env.dc.post(GENERAL, msg(NOW - timedelta(hours=10), "seed"))
    sid = await connected(env)
    await env.run(sid)
    env.dc.post(GENERAL, *(msg(NOW - timedelta(hours=9 - i), f"word{i}") for i in range(7)))
    for _ in range(4):
        await env.run(sid)
    for i in range(7):
        assert await env.find(f"word{i}")


async def test_the_connector_can_always_delete_their_own_source(env: Env) -> None:
    sid = await connected(env)
    demoted = Identity(
        **{**{f: getattr(env.me, f) for f in env.me.__dataclass_fields__}, "role": Role.MEMBER}
    )
    await env.svc.disconnect(demoted, sid)
    assert sid not in env.sources.records


async def test_running_syncs_report_progress(env: Env) -> None:
    env.dc.post(GENERAL, *(msg(NOW - timedelta(hours=40 - i), f"topic {i}") for i in range(12)))
    sid = await connected(env)
    await env.run(sid)
    assert env.sources.progressed  # every 10 items, so the source page shows live counts
    assert env.sources.progressed[0][1] == 10


# ================================================================== honest outcomes (P0-1)


async def test_a_crash_is_failed_and_re_raised_not_an_empty_success(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = await connected(env)

    async def boom(guild_id: str) -> None:
        raise ValueError("message text from the server")

    monkeypatch.setattr(env.dc, "guild", boom)
    with pytest.raises(ValueError, match="message text"):
        await env.run(sid)
    assert env.last_status() == "failed"
    summary = list(env.sources.finished.values())[-1][4] or ""
    assert "ValueError" in summary
    assert "message text" not in summary
    assert sid not in env.sources.synced


async def test_a_missing_guild_id_is_a_plain_failure(env: Env) -> None:
    sid = await connected(env)
    rec = env.sources.records[sid]
    for bad in ({}, {**rec.config, "guild_id": ""}, {**rec.config, "guild_id": 123}):
        env.sources.records[sid] = replace(rec, config=bad)
        assert await env.run(sid) == "failed"
        assert "settings are incomplete" in (list(env.sources.finished.values())[-1][4] or "")
