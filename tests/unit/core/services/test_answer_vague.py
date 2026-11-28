"""Vague questions end to end (ADR-0043), with the fakes: "I sent an email today to a
person, name him and see what I sent", "what is the document about?", stale sources,
relaxed filters, and files that can't be read."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta, tzinfo
from uuid import uuid4

from core.adapters.parsers.formats import FormatParser
from core.domain.attachments import AttachmentInfo, AttachmentState
from core.domain.auth import Identity
from core.domain.chat import ChatMessage
from core.domain.documents import AttachmentParent, DocumentUpsert, NewChunk
from core.domain.errors import RateLimited
from core.domain.ids import DocumentId, SourceId, UserId
from core.domain.principals import user_principal
from core.services.attachments import AttachmentIndexer, AttachmentJobs, AttachmentReader
from core.services.ingest import IngestService
from tests.fakes.jobs import FakeJobQueue
from tests.fakes.models import WordTokenizer
from tests.fakes.sources import InMemorySources
from tests.fakes.storage import InMemoryBlobStore
from tests.fakes.system import FakeClock
from tests.fakes.usage import InMemoryGenerations
from tests.unit.core.services.test_answer import OWNER, WS, Env, done, identity, is_planner

NOW = datetime(2026, 11, 27, 9, 0, tzinfo=UTC)
GMAIL = SourceId(uuid4())
DISCORD = SourceId(uuid4())
ME = user_principal(OWNER)


def scripted(plan: Mapping[str, object], answer: str = "You emailed Aashutosh [1].") -> object:
    def reply(messages: Sequence[ChatMessage]) -> str:
        return json.dumps(plan) if is_planner(messages) else answer

    return reply


def prompt_of(env: Env) -> str:
    request = next(r for r in reversed(env.model.requests) if not is_planner(r))
    return "\n".join(m.content for m in request)


async def put(
    env: Env,
    source: SourceId,
    ext: str,
    text: str,
    *,
    title: str,
    meta: Mapping[str, object],
    updated: datetime = NOW - timedelta(hours=1),
    parent: str | None = None,
    author: str = "Me <me@x.com>",
) -> DocumentId:
    doc_id, _ = await env.index.write_document(
        WS,
        DocumentUpsert(
            source, ext, f"mail:{ext}", title, "text/plain", ext + "-sum", (ME,), author=author,
            source_updated_at=updated, metadata=dict(meta), parent_external_id=parent,
        ),
        [NewChunk(0, text, len(text.split()))],
        {},
        data_class="private",
    )  # fmt: skip
    return doc_id


def pdf_info(state: AttachmentState = AttachmentState.TEXT) -> AttachmentInfo:
    return AttachmentInfo(key="s1-1", name="Aashutosh_Mali_CV.pdf", mime="application/pdf",
                          size=90_000, state=state, ref={"message": "m1", "part": "1"})  # fmt: skip


async def sent_email(
    env: Env, *, pdf_text: str | None = "Python developer, 4 years at Acme."
) -> None:
    env.index.connectors[GMAIL] = "gmail"
    state = AttachmentState.TEXT if pdf_text else AttachmentState.LISTED
    await put(
        env, GMAIL, "thread:t1",
        "From Me <me@x.com> on 2026-11-27 08:00 to Aashutosh <a@y.com>:\n(no text)\n"
        "[attachment: Aashutosh_Mali_CV.pdf (PDF, 87 KB)]",
        title="Test",
        meta={"kind": "email", "folders": ["sent"], "people": ["Me <me@x.com>",
              "Aashutosh <a@y.com>"], "attachments": [pdf_info(state).to_meta()]},
    )  # fmt: skip
    if pdf_text:
        await put(env, GMAIL, "thread:t1#att/s1-1", pdf_text,
                  title="Aashutosh_Mali_CV.pdf (attached to: Test)",
                  meta={"kind": "attachment"}, parent="thread:t1")  # fmt: skip


VAGUE = {
    "queries": ["email I sent today"], "intent": "recent", "after": "2026-11-27",
    "direction": "sent", "wants_content": True,
}  # fmt: skip
QUESTION = "I have sent an email today to a person, name him and see what i have sent"


# ---------------------------------------------------------------- the reported case


async def test_the_sent_email_its_recipient_and_its_attachment_reach_the_answer() -> None:
    env = Env(scripted(VAGUE))
    await sent_email(env)
    events = await env.ask(QUESTION)
    assert not done(events).abstained
    prompt = prompt_of(env)
    assert "to Aashutosh &lt;a@y.com>" in prompt  # who it was sent to ("<" is escaped)
    assert "Python developer, 4 years at Acme." in prompt  # what the PDF says
    assert "[Files: Aashutosh_Mali_CV.pdf (contents indexed)]" in prompt


async def test_matching_the_plans_filters_counts_as_evidence_whatever_the_similarity() -> None:
    plan = {**VAGUE, "intent": "lookup"}  # not the recent lane: the gate would normally apply
    env = Env(scripted(plan))
    env.svc._min_similarity = 0.99  # nothing resembles "(no text)"
    await sent_email(env)
    assert not done(await env.ask(QUESTION)).abstained


async def test_received_mail_is_not_offered_as_sent_mail() -> None:
    env = Env(scripted(VAGUE))
    env.index.connectors[GMAIL] = "gmail"
    await put(env, GMAIL, "thread:in", "From Priya to Me: invoice attached", title="Invoice",
              meta={"folders": ["inbox"], "people": ["Priya"]})  # fmt: skip
    events = await env.ask(QUESTION)
    notices = done(events).notices
    assert any(n.startswith("Nothing matched mail you sent") for n in notices)


async def test_a_file_that_cant_be_read_is_explained_not_guessed() -> None:
    env = Env(scripted(VAGUE))
    env.index.connectors[GMAIL] = "gmail"
    locked = AttachmentInfo(key="s1-1", name="contract.pdf", mime="application/pdf", size=10,
                            state=AttachmentState.ENCRYPTED)  # fmt: skip
    await put(env, GMAIL, "thread:t1", "From Me to Aashutosh: see contract", title="Contract",
              meta={"folders": ["sent"], "attachments": [locked.to_meta()]})  # fmt: skip
    await env.ask(QUESTION)
    assert "contract.pdf: password-protected, so it can't be read" in prompt_of(env)


# ---------------------------------------------------------------- on-demand reads


class Files:
    def __init__(self, data: Mapping[str, bytes | Exception | None], found: list[AttachmentInfo]):
        self.data = dict(data)
        self.found = found
        self.fetched: list[str] = []

    async def fetch(self, parent: AttachmentParent, info: AttachmentInfo) -> bytes | None:
        self.fetched.append(info.key)
        got = self.data.get(info.key)
        if isinstance(got, Exception):
            raise got
        return got

    async def discover(self, parent: AttachmentParent) -> list[AttachmentInfo]:
        return self.found


class Trigger:
    def __init__(self) -> None:
        self.reasons: list[str] = []

    async def trigger(self, reason: str) -> None:
        self.reasons.append(reason)


def with_reader(env: Env, files: Files, connector: str) -> tuple[FakeJobQueue, Trigger]:
    ingest = IngestService(
        InMemoryBlobStore(), env.index, None, None, WordTokenizer(), env.embedder,  # type: ignore[arg-type]
        InMemoryGenerations(), env.ledger,
        limits=env.svc._limits,
    )  # fmt: skip
    indexer = AttachmentIndexer(ingest, env.index, InMemorySources(env.index), FormatParser())
    reader = AttachmentReader(env.index, indexer, {connector: files})
    jobs = FakeJobQueue(FakeClock(NOW))
    trigger = Trigger()
    env.svc._attachments = reader
    env.svc._attachment_jobs = AttachmentJobs(reader, ingest, jobs)
    env.svc._trigger = trigger
    return jobs, trigger


async def test_an_old_discord_document_is_read_on_demand_to_answer_about_its_file() -> None:
    plan = {"queries": ["roma call transcript"], "wants_content": True}
    env = Env(scripted(plan, "The call covered pricing [1]."))
    env.index.connectors[DISCORD] = "discord"
    await put(env, DISCORD, f"msg:{1}:{2}", "Aashutosh (10:00): call notes\n  [attachment: "
              "Roma_Call_Transcript.txt]", title="#general", meta={"kind": "messages"},
              author="Aashutosh")  # fmt: skip
    transcript = AttachmentInfo(key="1400000000000000001", name="Roma_Call_Transcript.txt",
                                mime="text/plain", size=40)  # fmt: skip
    files = Files({transcript.key: b"Roma asked about pricing tiers and onboarding."}, [transcript])
    jobs, _ = with_reader(env, files, "discord")
    events = await env.ask("What were the main topics discussed in the call transcript?")
    assert not done(events).abstained
    assert "Roma asked about pricing tiers and onboarding." in prompt_of(env)
    assert files.fetched == [transcript.key]
    assert jobs.jobs  # its vectors are added by the worker (Tier 2)


async def test_a_read_that_cant_finish_now_is_handed_to_the_worker_and_said() -> None:
    env = Env(scripted(VAGUE))
    await sent_email(env, pdf_text=None)  # the PDF is only listed
    files = Files({"s1-1": RateLimited("slow", retry_after_s=5)}, [])
    jobs, trigger = with_reader(env, files, "gmail")
    events = await env.ask(QUESTION)
    assert any(n.startswith("Still reading Aashutosh_Mali_CV.pdf") for n in done(events).notices)
    assert trigger.reasons == ["attachments"]
    assert any(j.kind == "read_attachments" for j in jobs.jobs.values())


async def test_someone_else_never_triggers_reads_of_your_files() -> None:
    env = Env(scripted(VAGUE))
    await sent_email(env, pdf_text=None)
    files = Files({"s1-1": b"secret"}, [])
    with_reader(env, files, "gmail")
    other = identity(UserId(uuid4()))
    await env.ask(QUESTION, other)
    assert files.fetched == []


# ---------------------------------------------------------------- freshness


class Fresh:
    def __init__(self, notes: tuple[str, ...]) -> None:
        self.notes = notes
        self.asked: list[tuple[str, ...]] = []

    async def freshen(
        self, identity: Identity, connectors: Sequence[str], *, now: datetime, zone: tzinfo
    ) -> tuple[str, ...]:
        self.asked.append(tuple(connectors))
        return self.notes


async def test_a_question_about_today_checks_the_mail_sources_first() -> None:
    env = Env(scripted(VAGUE))
    note = "Gmail last synced at 07:17 (103 minutes ago), so newer items may not be in Slate yet."
    fresh = Fresh((note,))
    env.svc._freshness = fresh
    await sent_email(env)
    events = await env.ask(QUESTION)
    assert fresh.asked == [("imap", "gmail_mbox", "gmail")]
    assert note in done(events).notices


async def test_questions_not_about_the_last_hours_dont_sync_anything() -> None:
    env = Env(scripted({"queries": ["leave policy"]}))
    fresh = Fresh(())
    env.svc._freshness = fresh
    await env.ask("what is the leave policy?")
    assert fresh.asked == []


async def test_a_failing_freshness_check_never_fails_the_answer() -> None:
    env = Env(scripted(VAGUE))

    class Broken(Fresh):
        async def freshen(self, *a: object, **k: object) -> tuple[str, ...]:
            raise RuntimeError("database down")

    env.svc._freshness = Broken(())
    await sent_email(env)
    assert not done(await env.ask(QUESTION)).abstained
