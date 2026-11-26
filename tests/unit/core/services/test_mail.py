"""MailService (connect), MailSync (live IMAP) and MboxImport, against in-memory fakes."""

from __future__ import annotations

import hashlib
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from core.adapters.mail.parse import parse_mbox
from core.adapters.parsers.formats import FormatParser
from core.domain.auth import Identity, Role
from core.domain.budget import SpendLimits
from core.domain.documents import SearchFilters
from core.domain.errors import (
    Conflict,
    PermissionDenied,
    RateLimited,
    UpstreamUnavailable,
    ValidationFailed,
)
from core.domain.ids import JobId, SessionId, SourceId, UserId, WorkspaceId
from core.domain.jobs import Job, JobStatus
from core.domain.mail import FolderCursor, Mail, MailCheckpoint, Provider
from core.domain.principals import Principals, user_principal
from core.services.ingest import IngestService
from core.services.mail import (
    IMAP,
    MBOX,
    SYNC_MAIL,
    MailService,
    MailSync,
    MboxActions,
    MboxImport,
    mbox_source,
)
from tests.fakes.credentials import InMemoryCredentials
from tests.fakes.jobs import FakeJobQueue
from tests.fakes.mailbox import FakeMailbox
from tests.fakes.models import FakeEmbedder, WordTokenizer
from tests.fakes.rate_limit import InMemoryRateLimiter
from tests.fakes.sources import InMemorySources
from tests.fakes.storage import InMemoryBlobStore, InMemoryIndex
from tests.fakes.system import FakeClock
from tests.fakes.usage import InMemoryGenerations, InMemoryLedger

NOW = datetime(2026, 11, 26, 12, tzinfo=UTC)
PASSWORD = "abcdefghijklmnop"
_n = iter(range(1, 10**6))


def identity(role: Role = Role.OWNER, ws: WorkspaceId | None = None) -> Identity:
    u, w = UserId(uuid4()), ws or WorkspaceId(uuid4())
    return Identity(
        user_id=u, workspace_id=w, session_id=SessionId(uuid4()), email="me@gmail.com",
        name="Me", role=role, principals=Principals.for_user(u, w), csrf_secret="c",
        email_verified=True,
    )  # fmt: skip


def mail(text: str, *, ago: timedelta, subject: str = "Lease", refs: tuple[str, ...] = (),
         thread: str | None = None) -> Mail:  # fmt: skip
    n = next(_n)
    return Mail(
        uid=0, folder="", message_id=f"<m{n}@x.test>", subject=subject,
        sender="Landlord <l@x.test>", to=("me@gmail.com",), date=NOW - ago, body=text,
        references=refs, gm_thread=thread,
    )  # fmt: skip


class Env:
    def __init__(self) -> None:
        self.clock = FakeClock(NOW)
        self.box = FakeMailbox(password=PASSWORD)
        self.creds = InMemoryCredentials()
        self.index = InMemoryIndex()
        self.sources = InMemorySources(self.index)
        self.jobs = FakeJobQueue(self.clock)
        self.ledger = InMemoryLedger()
        self.embedder = FakeEmbedder(dims=16)
        self.svc = MailService(
            self.box, self.creds, self.sources, self.jobs, InMemoryRateLimiter(self.clock),
            self.clock,
        )  # fmt: skip
        self.ingest = IngestService(
            InMemoryBlobStore(), self.index, self.sources, FormatParser(), WordTokenizer(),
            self.embedder, InMemoryGenerations(), self.ledger,
            limits=SpendLimits.monthly_only(Decimal(5)),
        )  # fmt: skip
        self.sync = MailSync(self.box, self.creds, self.sources, self.jobs, self.ingest, self.clock)
        self.me = identity()

    async def connect(self, who: Identity | None = None) -> SourceId:
        done = await self.svc.connect(
            who or self.me, provider=Provider.GMAIL, address="me@gmail.com",
            password="abcd efgh ijkl mnop",
        )  # fmt: skip
        return done.source_id

    def job(self, sid: SourceId, ws: WorkspaceId | None = None) -> Job:
        return Job(
            id=JobId(uuid4()), workspace_id=ws or self.me.workspace_id, kind=SYNC_MAIL,
            payload={"source_id": str(sid)}, status=JobStatus.RUNNING, attempts=1,
            max_attempts=5, run_after=NOW, idempotency_key="k",
        )  # fmt: skip

    async def run(self, sid: SourceId, *, deadline: float | None = None) -> str:
        await self.sync.sync(self.job(sid), deadline=deadline)
        return list(self.sources.finished.values())[-1][0]

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


async def test_connect_checks_login_and_stores_the_password_encrypted(env: Env) -> None:
    sid = await env.connect()
    rec = env.sources.records[sid]
    assert rec.connector == IMAP
    assert rec.visibility == (user_principal(env.me.user_id),)
    assert rec.data_class == "private"
    assert rec.config["address"] == "me@gmail.com"
    assert "abcd" not in str(rec.config)  # the password is never in config
    assert rec.credential_id is not None
    assert await env.creds.secret(env.me.workspace_id, rec.credential_id) == PASSWORD
    (job,) = env.jobs.jobs.values()
    assert job.idempotency_key.endswith(":initial")


async def test_wrong_password_stores_nothing(env: Env) -> None:
    with pytest.raises(PermissionDenied):
        await env.svc.connect(
            env.me, provider=Provider.GMAIL, address="me@gmail.com", password="wrongwrongwrong"
        )
    assert env.sources.records == {}
    assert env.creds.rows == {}


async def test_login_attempts_are_rate_limited(env: Env) -> None:
    for _ in range(5):
        with pytest.raises(PermissionDenied):
            await env.svc.connect(
                env.me, provider=Provider.GMAIL, address="me@gmail.com", password="x" * 12
            )
    with pytest.raises(RateLimited):
        await env.connect()
    assert len(env.box.logins) == 5  # the 6th never reached the mail server


@pytest.mark.parametrize(
    ("address", "password"),
    [("nope", PASSWORD), ("me@gmail.com", "short"), ("me@gmail.com", "x" * 300)],
)
async def test_connect_validates_input(env: Env, address: str, password: str) -> None:
    with pytest.raises(ValidationFailed):
        await env.svc.connect(env.me, provider=Provider.GMAIL, address=address, password=password)
    assert env.box.logins == []


async def test_same_mailbox_twice_is_a_conflict(env: Env) -> None:
    await env.connect()
    with pytest.raises(Conflict):
        await env.connect()


async def test_unavailable_without_an_encryption_key(env: Env) -> None:
    svc = MailService(
        env.box, InMemoryCredentials(available=False), env.sources, env.jobs,
        InMemoryRateLimiter(env.clock), env.clock,
    )  # fmt: skip
    with pytest.raises(UpstreamUnavailable):
        await svc.connect(
            env.me, provider=Provider.GMAIL, address="me@gmail.com", password=PASSWORD
        )


async def test_disconnect_deletes_the_credential(env: Env) -> None:
    sid = await env.connect()
    await env.svc.disconnect(env.me, sid)
    assert sid not in env.sources.records
    assert env.creds.rows == {}


async def test_viewer_cannot_connect(env: Env) -> None:
    with pytest.raises(PermissionDenied):
        await env.connect(identity(Role.VIEWER))


# ================================================================== sync


async def test_first_sync_indexes_90_days_of_inbox_and_sent(env: Env) -> None:
    env.box.deliver("INBOX", mail("the deposit is 1200 euros", ago=timedelta(days=3)))
    env.box.deliver(
        "Sent", mail("thanks for the invoice", ago=timedelta(days=2), subject="Invoice")
    )
    env.box.deliver("INBOX", mail("ancient newsletter", ago=timedelta(days=200), subject="Old"))
    sid = await env.connect()
    assert await env.run(sid) == "succeeded"
    assert await env.find("deposit")
    assert await env.find("invoice")
    assert not await env.find("ancient")
    assert env.ledger.entries[0].data_class == "private"


async def test_mail_is_invisible_to_everyone_else(env: Env) -> None:
    env.box.deliver("INBOX", mail("medical results attached", ago=timedelta(days=1)))
    sid = await env.connect()
    await env.run(sid)
    assert await env.find("medical")
    assert await env.find("medical", identity(ws=env.me.workspace_id)) == []  # same workspace
    assert await env.find("medical", identity()) == []  # another account


async def test_a_reply_rebuilds_the_whole_thread_as_one_document(env: Env) -> None:
    root = env.box.deliver("INBOX", mail("when can I move in?", ago=timedelta(days=5)))
    sid = await env.connect()
    await env.run(sid)
    env.box.deliver(
        "Sent",
        mail(
            "keys on Monday", ago=timedelta(hours=1), subject="Re: Lease", refs=(root.message_id,)
        ),
    )
    await env.run(sid)
    docs = env.index.checksums(sid, "thread:")
    assert len(docs) == 1  # one conversation, both messages
    assert await env.find("keys")
    assert await env.find("move")  # both messages are in that one document


async def test_unchanged_mailbox_costs_nothing_on_resync(env: Env) -> None:
    env.box.deliver("INBOX", mail("rent reminder", ago=timedelta(days=1)))
    sid = await env.connect()
    await env.run(sid)
    calls = len(env.embedder.calls)
    fetched = env.box.fetched
    assert await env.run(sid) == "succeeded"
    assert len(env.embedder.calls) == calls
    assert env.box.fetched == fetched  # incremental: nothing new to fetch


async def test_gmail_thread_ids_group_across_folders(env: Env) -> None:
    env.box.deliver("INBOX", mail("question about parking", ago=timedelta(days=2), thread="77"))
    env.box.deliver(
        "Sent", mail("answer about parking", ago=timedelta(days=1), thread="77", subject="Re: x")
    )
    sid = await env.connect()
    await env.run(sid)
    assert len(env.index.checksums(sid, "thread:")) == 1


async def test_uidvalidity_change_rereads_without_duplicates(env: Env) -> None:
    env.box.deliver("INBOX", mail("garden schedule", ago=timedelta(days=1)))
    sid = await env.connect()
    await env.run(sid)
    env.box.folders["INBOX"].uidvalidity = 99  # the server rebuilt the folder
    assert await env.run(sid) == "succeeded"
    assert len(env.index.checksums(sid, "thread:")) == 1
    cp = MailCheckpoint.from_json(env.sources.records[sid].checkpoint)
    assert cp.cursor("INBOX").uidvalidity == 99


async def test_bad_saved_password_fails_with_reconnect_advice(env: Env) -> None:
    sid = await env.connect()
    env.box.password = "changed-by-user"
    assert await env.run(sid) == "failed"
    assert "app password" in (list(env.sources.finished.values())[-1][4] or "")


async def test_lost_credential_fails_cleanly(env: Env) -> None:
    sid = await env.connect()
    env.creds.rows.clear()  # e.g. the encryption key was rotated away
    assert await env.run(sid) == "failed"


async def test_server_down_fails_and_keeps_cursor(env: Env) -> None:
    env.box.deliver("INBOX", mail("first", ago=timedelta(days=1)))
    sid = await env.connect()
    await env.run(sid)
    before = env.sources.records[sid].checkpoint
    env.box.deliver("INBOX", mail("second", ago=timedelta(hours=1)))
    env.box.down = True
    assert await env.run(sid) == "failed"
    assert env.sources.records[sid].checkpoint == before
    env.box.down = False
    await env.run(sid)
    assert await env.find("second")


async def test_budget_stop_keeps_cursors_and_resumes(env: Env) -> None:
    env.box.deliver("INBOX", mail("costly words", ago=timedelta(days=1)))
    sid = await env.connect()
    env.ledger.spent = Decimal(10)
    assert await env.run(sid) == "partial"
    assert MailCheckpoint.from_json(env.sources.records[sid].checkpoint).first_sync
    env.ledger.spent = Decimal(0)
    assert await env.run(sid) == "succeeded"
    assert await env.find("costly")


async def test_deadline_continues_in_the_background(env: Env) -> None:
    env.box.deliver("INBOX", mail("later", ago=timedelta(days=1)))
    sid = await env.connect()
    env.jobs.jobs.clear()
    assert await env.run(sid, deadline=time.monotonic() - 1) == "partial"
    (cont,) = env.jobs.jobs.values()
    assert ":cont:" in cont.idempotency_key


async def test_job_for_another_workspace_is_skipped(env: Env) -> None:
    sid = await env.connect()
    await env.sync.sync(env.job(sid, WorkspaceId(uuid4())), deadline=None)
    assert env.sources.runs == {}


async def test_checkpoint_stores_folder_cursors(env: Env) -> None:
    env.box.deliver("INBOX", mail("x", ago=timedelta(days=1)))
    sid = await env.connect()
    await env.run(sid)
    cp = MailCheckpoint.from_json(env.sources.records[sid].checkpoint)
    assert cp.cursor("INBOX") == FolderCursor(1, 1)


# ================================================================== .mbox import


def _mbox(*bodies: str) -> bytes:
    out = b""
    for i, body in enumerate(bodies):
        out += (
            f"From x@x Fri Nov 20 10:00:00 +0000 2026\nFrom: a@x.test\nSubject: S{i}\n"
            f"Message-ID: <mb{i}@x>\nDate: Fri, 20 Nov 2026 10:0{i}:00 +0000\n\n{body}\n\n"
        ).encode()
    return out


async def _import_env(env: Env, data: bytes) -> tuple[MboxImport, Job, InMemoryBlobStore]:
    blobs = InMemoryBlobStore()
    key = f"uploads/{env.me.workspace_id}/{env.me.user_id}/{hashlib.sha256(data).hexdigest()}.mbox"
    await blobs.put(key, data, "application/mbox")
    sid = await mbox_source(env.sources, env.me)
    job = Job(
        id=JobId(uuid4()), workspace_id=env.me.workspace_id, kind="import_mbox",
        payload={"blob_key": key, "sha256": hashlib.sha256(data).hexdigest(),
                 "source_id": str(sid), "uploader": str(env.me.user_id)},
        status=JobStatus.RUNNING, attempts=1, max_attempts=5, run_after=NOW, idempotency_key="k",
    )  # fmt: skip
    return MboxImport(blobs, env.sources, env.ingest, parse_mbox), job, blobs


async def test_mbox_import_indexes_threads_and_deletes_the_file(env: Env) -> None:
    data = _mbox("plumber comes tuesday", "insurance renewal")
    imp, job, blobs = await _import_env(env, data)
    await imp.handle(job)
    assert await env.find("plumber")
    assert await env.find("insurance")
    assert blobs.blobs == {}  # raw mail isn't kept
    src = env.sources.records[next(iter(env.sources.records))]
    assert src.connector == MBOX
    assert src.visibility == (user_principal(env.me.user_id),)


async def test_mbox_import_refuses_a_swapped_file(env: Env) -> None:
    imp, job, blobs = await _import_env(env, _mbox("x"))
    key = str(job.payload["blob_key"])
    blobs.blobs[key] = (b"From evil\n\nswapped", "application/mbox")
    await imp.handle(job)
    assert list(env.sources.finished.values())[-1][0] == "failed"
    assert not await env.find("swapped")


async def test_mbox_import_refuses_someone_elses_object(env: Env) -> None:
    imp, job, _ = await _import_env(env, _mbox("x"))
    forged = replace(
        job, payload={**job.payload, "blob_key": f"uploads/{env.me.workspace_id}/{uuid4()}/a.mbox"}
    )
    with pytest.raises(ValidationFailed):
        await imp.handle(forged)


async def test_mbox_sources_cannot_be_synced(env: Env) -> None:
    sid = await mbox_source(env.sources, env.me)
    with pytest.raises(ValidationFailed):
        await MboxActions(env.svc).sync_now(env.me, sid)
    await MboxActions(env.svc).disconnect(env.me, sid)
    assert sid not in env.sources.records


async def test_non_ascii_password_is_refused_before_any_login(env: Env) -> None:
    with pytest.raises(ValidationFailed):
        await env.svc.connect(
            env.me, provider=Provider.GMAIL, address="me@gmail.com", password="pässwörd-pässwörd"
        )
    assert env.box.logins == []


async def test_a_permanent_failure_does_not_hold_the_cursor(env: Env) -> None:
    env.box.deliver("INBOX", mail("good mail", ago=timedelta(days=1), subject="A"))
    env.box.deliver("INBOX", mail("bad mail", ago=timedelta(hours=2), subject="B"))
    sid = await env.connect()
    real = env.ingest.index_document

    async def reject_b(ws, doc, sections, **kw):
        if doc.title == "B":
            raise ValidationFailed("No text found in this file.")  # not retryable
        return await real(ws, doc, sections, **kw)

    env.ingest.index_document = reject_b
    assert await env.run(sid) == "partial"
    cp = MailCheckpoint.from_json(env.sources.records[sid].checkpoint)
    assert cp.cursor("INBOX").last_uid == 2  # moved on: re-reading would fail the same way


async def test_an_unexpected_crash_is_a_failed_run_not_a_success(env: Env) -> None:
    sid = await env.connect()

    async def boom(*a: object, **k: object) -> dict[str, str]:
        raise RuntimeError("bug")

    env.sources.document_checksums = boom
    with pytest.raises(RuntimeError):
        await env.sync.sync(env.job(sid), deadline=None)
    assert list(env.sources.finished.values())[-1][0] == "failed"
    assert env.sources.synced == []


async def test_a_failed_source_create_leaves_no_credential(env: Env) -> None:
    async def broken(*a: object, **k: object) -> SourceId:
        raise RuntimeError("db down")

    env.sources.create_source = broken
    with pytest.raises(RuntimeError):
        await env.connect()
    assert env.creds.rows == {}
