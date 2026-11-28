"""Attachments (ADR-0043): bytes -> state -> child document, sync and on demand.

Each test names the edge case it covers (docs/DECISIONS.md ADR-0043, plan catalogue).
"""

from __future__ import annotations

import io
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from core.adapters.parsers.formats import FormatParser
from core.domain.attachments import (
    MAX_ATTACHMENT_BYTES,
    AttachmentBlob,
    AttachmentInfo,
    AttachmentState,
    child_external_id,
    from_meta,
    key_seed,
)
from core.domain.budget import SpendLimits
from core.domain.documents import (
    AttachmentParent,
    DocumentUpsert,
    ParsedDocument,
    SearchFilters,
    Section,
)
from core.domain.errors import RateLimited, UnreadableFile
from core.domain.ids import DocumentId, SourceId, WorkspaceId
from core.domain.principals import Principals, user_principal
from core.services.attachments import (
    READ_ATTACHMENTS,
    AttachmentIndexer,
    AttachmentJobs,
    AttachmentReader,
    ParentRef,
    Prepared,
)
from core.services.ingest import IngestService
from tests.fakes.jobs import FakeJobQueue
from tests.fakes.models import FakeEmbedder, WordTokenizer
from tests.fakes.sources import InMemorySources
from tests.fakes.storage import InMemoryBlobStore, InMemoryIndex
from tests.fakes.system import FakeClock
from tests.fakes.usage import InMemoryGenerations, InMemoryLedger

WS = WorkspaceId(uuid4())
SRC = SourceId(uuid4())
ADA = user_principal(uuid4())  # type: ignore[arg-type]
EVE = user_principal(uuid4())  # type: ignore[arg-type]
NOW = datetime(2026, 11, 28, 14, 5, tzinfo=UTC)
PARENT = "thread:abc"


def docx_bytes(*paragraphs: str) -> bytes:
    import docx

    d = docx.Document()
    for p in paragraphs:
        d.add_paragraph(p)
    out = io.BytesIO()
    d.save(out)
    return out.getvalue()


def info(name: str, mime: str = "application/octet-stream", size: int = 10, key: str = "k1-0"):
    return AttachmentInfo(key=key, name=name, mime=mime, size=size, ref={"part": "0"})


class StubParser:
    """A parser that fails the way a real one would for files tests can't easily build."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    def supports(self, mime: str) -> bool:
        return True

    def parse(self, data: bytes, mime: str) -> ParsedDocument:
        if self.error is not None:
            raise self.error
        return ParsedDocument(None, (Section(None, data.decode()),))


class Env:
    def __init__(self, parser: object | None = None, *, spent: Decimal = Decimal(0)) -> None:
        self.index = InMemoryIndex()
        self.index.connectors[SRC] = "discord"
        self.sources = InMemorySources(self.index)
        self.ledger = InMemoryLedger(spent)
        self.embedder = FakeEmbedder(dims=16)
        self.ingest = IngestService(
            InMemoryBlobStore(), self.index, self.sources, FormatParser(), WordTokenizer(),
            self.embedder, InMemoryGenerations(), self.ledger,
            limits=SpendLimits.monthly_only(Decimal(5)),
        )  # fmt: skip
        self.engine = AttachmentIndexer(
            self.ingest,
            self.index,
            self.sources,
            parser or FormatParser(),  # type: ignore[arg-type]
        )

    async def parent(self, attachments: list[AttachmentInfo] | None = None) -> DocumentId:
        meta: dict[str, object] = {"kind": "messages"}
        if attachments is not None:
            meta["attachments"] = [a.to_meta() for a in attachments]
        result = await self.ingest.index_document(
            WS,
            DocumentUpsert(
                source_id=SRC, external_id=PARENT, uri="https://discord.test/1", title="Test",
                mime="text/plain", checksum="p1", acl=(ADA,), author="Aashutosh",
                source_updated_at=NOW, metadata=meta,
            ),
            [Section("Test", "Aashutosh (14:05): see attached\n  [attachment: notes.docx]")],
            data_class="private",
        )  # fmt: skip
        return result.document_id

    def ref(self, acl: tuple[str, ...] = (ADA,)) -> ParentRef:
        return ParentRef(
            workspace_id=WS, source_id=SRC, external_id=PARENT, title="Test",
            uri="https://discord.test/1", acl=acl, data_class="private", updated_at=NOW,
        )  # fmt: skip


# ---------------------------------------------------------------- reading bytes (Tier 1)


async def test_a_word_file_becomes_text_sections_named_after_the_file() -> None:
    env = Env()
    p = await env.engine.read(AttachmentBlob(info("notes.docx"), docx_bytes("Roadmap for Q1")))
    assert p.info.state is AttachmentState.TEXT
    assert p.sections[0].heading_path == "notes.docx"
    assert "Roadmap for Q1" in p.sections[0].text
    assert len(p.checksum) == 64


async def test_unsupported_types_are_named_not_guessed() -> None:  # edge case 1
    env = Env()
    for name in ("photo.png", "archive.zip", "sheet.xlsx", "talk.mp3"):
        p = await env.engine.read(AttachmentBlob(info(name), b"\x89PNG...."))
        assert p.info.state is AttachmentState.UNSUPPORTED, name


async def test_a_file_whose_bytes_contradict_its_name_is_unreadable() -> None:  # edge case 2
    env = Env()
    p = await env.engine.read(AttachmentBlob(info("invoice.pdf"), b"<html><script>x</script>"))
    assert p.info.state is AttachmentState.UNREADABLE
    assert p.sections == ()


async def test_declared_type_never_overrides_the_bytes() -> None:  # edge case 2
    env = Env()
    blob = AttachmentBlob(info("report", "application/pdf"), b"MZ\x90\x00 an executable")
    assert (await env.engine.read(blob)).info.state is AttachmentState.UNREADABLE


async def test_too_large_files_are_not_parsed() -> None:  # edge case 3
    env = Env(StubParser(AssertionError("must not parse")))
    big = AttachmentBlob(info("big.txt", size=1), b"x" * (MAX_ATTACHMENT_BYTES + 1))
    assert (await env.engine.read(big)).info.state is AttachmentState.TOO_LARGE
    listed = info("big.txt", size=MAX_ATTACHMENT_BYTES + 1)
    [p] = await env.engine.prepare([listed], [])
    assert p.info.state is AttachmentState.TOO_LARGE


async def test_password_protected_word_files_are_reported_as_encrypted() -> None:  # edge case 4
    env = Env()
    ole = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64
    p = await env.engine.read(AttachmentBlob(info("secret.docx"), ole))
    assert p.info.state is AttachmentState.ENCRYPTED


@pytest.mark.parametrize(
    ("reason", "state"),
    [
        ("encrypted", AttachmentState.ENCRYPTED),  # edge case 4 (PDF password)
        ("empty", AttachmentState.EMPTY),  # edge case 5 (scan, no text layer)
        ("too_large", AttachmentState.TOO_LARGE),  # edge case 3 (pages, text cap)
        ("unreadable", AttachmentState.UNREADABLE),  # damaged
    ],
)
async def test_parser_reasons_become_states(reason: str, state: AttachmentState) -> None:
    env = Env(StubParser(UnreadableFile("no", reason=reason)))
    p = await env.engine.read(AttachmentBlob(info("a.txt"), b"some text"))
    assert p.info.state is state


async def test_a_parse_that_hangs_times_out_as_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # 6
    import core.services.attachments as mod

    monkeypatch.setattr(mod, "PARSE_TIMEOUT_S", 0.05)

    class Slow(StubParser):
        def parse(self, data: bytes, mime: str) -> ParsedDocument:
            import time

            time.sleep(0.3)
            return super().parse(data, mime)

    env = Env(Slow())
    p = await env.engine.read(AttachmentBlob(info("a.txt"), b"text"))
    assert p.info.state is AttachmentState.UNREADABLE


async def test_whitespace_only_text_is_empty() -> None:
    env = Env(StubParser())
    p = await env.engine.read(AttachmentBlob(info("a.txt"), b"   \n  "))
    assert p.info.state is AttachmentState.EMPTY


async def test_zero_bytes_is_empty() -> None:
    env = Env()
    assert (await env.engine.read(AttachmentBlob(info("a.txt"), b""))).info.state is (
        AttachmentState.EMPTY
    )


async def test_a_forwarded_email_is_read_as_text() -> None:  # edge case 9
    env = Env()
    blob = AttachmentBlob(info("Fwd thing.eml", "message/rfc822"), b"From z on 2026: inner body")
    p = await env.engine.read(blob)
    assert p.info.state is AttachmentState.TEXT
    assert "inner body" in p.sections[0].text


async def test_files_without_kept_bytes_stay_listed_for_on_demand() -> None:
    env = Env()
    [p] = await env.engine.prepare([info("notes.docx")], [])
    assert p.info.state is AttachmentState.LISTED


# ---------------------------------------------------------------- children (database rules)


async def test_a_readable_file_becomes_a_child_with_the_parents_access() -> None:
    env = Env()
    parent = await env.parent()
    prepared = await env.engine.prepare(
        [info("notes.docx")], [AttachmentBlob(info("notes.docx"), docx_bytes("Budget 4200"))]
    )
    final = await env.engine.write(env.ref(acl=(EVE,)), prepared)  # the caller lies
    assert final[0].state is AttachmentState.TEXT
    kids = await env.index.attachment_children(WS, Principals((ADA,)), [parent])
    assert len(kids) == 1
    # Eve can't see the child: it carries the parent's ACL, not the one passed in.
    assert await env.index.attachment_children(WS, Principals((EVE,)), [parent]) == []
    hits = await env.index.keyword_search(WS, Principals((ADA,)), "4200", 5, _filters())
    assert hits
    assert hits[0].parent_id == parent
    assert "attached to: Test" in (hits[0].title or "")


async def test_children_have_no_vectors_until_asked() -> None:  # Tier 1 is free
    env = Env()
    await env.parent()
    calls, spent = list(env.embedder.calls), len(env.ledger.entries)
    prepared = await env.engine.prepare(
        [info("a.txt")], [AttachmentBlob(info("a.txt"), b"alpha beta")]
    )
    await env.engine.write(env.ref(), prepared)
    assert env.embedder.calls == calls
    assert len(env.ledger.entries) == spent


async def test_rewriting_the_same_file_changes_nothing() -> None:  # idempotent (rule 3)
    env = Env()
    await env.parent()
    blob = AttachmentBlob(info("a.txt"), b"alpha beta")
    for _ in range(2):
        await env.engine.write(env.ref(), await env.engine.prepare([blob.info], [blob]))
    ext = child_external_id(PARENT, "k1-0")
    assert list((await env.index.child_checksums(SRC, PARENT)).keys()) == [ext]


async def test_a_file_removed_from_the_conversation_is_pruned() -> None:
    env = Env()
    await env.parent()
    a = AttachmentBlob(info("a.txt", key="k1-0"), b"alpha")
    b = AttachmentBlob(info("b.txt", key="k1-1"), b"beta")
    await env.engine.write(env.ref(), await env.engine.prepare([a.info, b.info], [a, b]))
    await env.engine.write(env.ref(), await env.engine.prepare([a.info], [a]))
    assert list(await env.index.child_checksums(SRC, PARENT)) == [child_external_id(PARENT, "k1-0")]


async def test_children_are_not_in_a_syncs_listing_so_prunes_never_hit_them() -> None:
    env = Env()
    await env.parent()
    a = AttachmentBlob(info("a.txt"), b"alpha")
    await env.engine.write(env.ref(), await env.engine.prepare([a.info], [a]))
    assert list(await env.sources.document_checksums(SRC, "")) == [PARENT]


async def test_deleting_the_parent_deletes_its_children() -> None:
    env = Env()
    await env.parent()
    a = AttachmentBlob(info("a.txt"), b"alpha")
    await env.engine.write(env.ref(), await env.engine.prepare([a.info], [a]))
    await env.sources.delete_documents(SRC, [PARENT])
    hits = await env.index.keyword_search(WS, Principals((ADA,)), "alpha", 5, _filters())
    assert hits == []


async def test_a_failed_child_write_is_listed_not_claimed_as_text() -> None:
    env = Env()
    await env.parent()

    async def boom(*a: object, **k: object) -> object:
        raise RuntimeError("database hiccup")

    env.engine._ingest.index_document = boom  # type: ignore[method-assign]
    a = AttachmentBlob(info("a.txt"), b"alpha")
    final = await env.engine.write(env.ref(), await env.engine.prepare([a.info], [a]))
    assert final[0].state is AttachmentState.LISTED
    [stored] = from_meta(env.index.metadata[await _id(env, PARENT)]["attachments"])
    assert stored.state is AttachmentState.LISTED  # the parent was corrected


async def test_a_lost_race_with_the_same_bytes_counts_as_text() -> None:  # edge case 17
    env = Env()
    await env.parent()
    a = AttachmentBlob(info("a.txt"), b"alpha")
    prepared = await env.engine.prepare([a.info], [a])
    await env.engine.write(env.ref(), prepared)  # the "other" request won
    real = env.engine._ingest.index_document

    async def collide(*args: object, **kw: object) -> object:
        raise RuntimeError("unique violation")

    env.engine._ingest.index_document = collide  # type: ignore[method-assign]
    final = await env.engine.write(env.ref(), prepared)
    env.engine._ingest.index_document = real
    assert final[0].state is AttachmentState.TEXT


async def test_long_files_are_truncated_and_marked() -> None:  # edge case 3
    import core.services.attachments as mod

    env = Env()
    await env.parent()
    words = " ".join(f"w{i}" for i in range(500))
    a = AttachmentBlob(info("long.txt"), "\n\n".join([words] * 40).encode())
    old = mod.MAX_ATTACHMENT_TOKENS
    mod.MAX_ATTACHMENT_TOKENS = 1000
    try:
        final = await env.engine.write(env.ref(), await env.engine.prepare([a.info], [a]))
    finally:
        mod.MAX_ATTACHMENT_TOKENS = old
    assert final[0].state is AttachmentState.TEXT
    assert final[0].truncated


# ---------------------------------------------------------------- on demand


class Fetcher:
    def __init__(self, files: Mapping[str, bytes | Exception | None], found: list[AttachmentInfo]):
        self.files = dict(files)
        self.found = found
        self.calls: list[str] = []

    async def fetch(self, parent: AttachmentParent, info: AttachmentInfo) -> bytes | None:
        self.calls.append(info.key)
        got = self.files.get(info.key)
        if isinstance(got, Exception):
            raise got
        return got

    async def discover(self, parent: AttachmentParent) -> list[AttachmentInfo]:
        return self.found


def reader(env: Env, fetcher: Fetcher, now: list[float] | None = None) -> AttachmentReader:
    clock = now or [0.0]
    return AttachmentReader(env.index, env.engine, {"discord": fetcher}, clock=lambda: clock[0])


async def test_on_demand_reads_a_listed_file_and_records_it() -> None:
    env = Env()
    parent = await env.parent([info("notes.docx")])
    f = Fetcher({"k1-0": docx_bytes("Q1 roadmap launch in March")}, [])
    out = await reader(env, f).read(WS, Principals((ADA,)), parent, deadline=None)
    assert [a.state for a in out.attachments] == [AttachmentState.TEXT]
    assert not out.pending
    hits = await env.index.keyword_search(WS, Principals((ADA,)), "roadmap", 5, _filters())
    assert hits
    assert hits[0].parent_id == parent


async def test_documents_indexed_before_files_were_listed_are_discovered() -> None:
    env = Env()
    parent = await env.parent(None)  # the old Discord document: no metadata
    f = Fetcher({"k1-0": b"transcript text"}, [info("call.txt")])
    out = await reader(env, f).read(WS, Principals((ADA,)), parent, deadline=None)
    assert out.attachments[0].state is AttachmentState.TEXT
    stored = from_meta(env.index.metadata[parent]["attachments"])
    assert [s.name for s in stored] == ["call.txt"]


async def test_someone_who_cant_see_the_parent_reads_nothing() -> None:  # edge case 30
    env = Env()
    parent = await env.parent([info("notes.docx")])
    f = Fetcher({"k1-0": b"secret"}, [])
    out = await reader(env, f).read(WS, Principals((EVE,)), parent, deadline=None)
    assert out.attachments == ()
    assert f.calls == []


async def test_a_deleted_file_is_gone() -> None:  # edge case 13
    env = Env()
    parent = await env.parent([info("notes.docx")])
    out = await reader(env, Fetcher({"k1-0": None}, [])).read(
        WS, Principals((ADA,)), parent, deadline=None
    )
    assert out.attachments[0].state is AttachmentState.GONE


async def test_rate_limits_keep_the_file_listed_and_pending() -> None:  # edge case 16
    env = Env()
    parent = await env.parent([info("notes.docx")])
    f = Fetcher({"k1-0": RateLimited("slow down", retry_after_s=5)}, [])
    out = await reader(env, f).read(WS, Principals((ADA,)), parent, deadline=None)
    assert out.attachments[0].state is AttachmentState.LISTED
    assert out.pending


async def test_past_the_deadline_files_wait_for_the_worker() -> None:  # edge case 32
    env = Env()
    files = [info(f"f{i}.txt", key=f"k1-{i}") for i in range(2)]
    parent = await env.parent(files)
    now = [10.0]
    f = Fetcher({"k1-0": b"a", "k1-1": b"b"}, [])
    out = await reader(env, f, now).read(WS, Principals((ADA,)), parent, deadline=5.0)
    assert out.pending
    assert f.calls == []


async def test_at_most_a_few_files_per_request() -> None:
    env = Env()
    files = [info(f"f{i}.txt", key=f"k1-{i}") for i in range(5)]
    parent = await env.parent(files)
    f = Fetcher({fi.key: b"text" for fi in files}, [])
    out = await reader(env, f).read(WS, Principals((ADA,)), parent, deadline=None, max_files=2)
    assert len(f.calls) == 2
    assert out.pending


async def test_already_read_files_are_not_fetched_again() -> None:
    env = Env()
    parent = await env.parent([info("a.txt")])
    f = Fetcher({"k1-0": b"alpha"}, [])
    r = reader(env, f)
    await r.read(WS, Principals((ADA,)), parent, deadline=None)
    await r.read(WS, Principals((ADA,)), parent, deadline=None)
    assert f.calls == ["k1-0"]


# ---------------------------------------------------------------- the worker job (Tier 2)


async def test_the_job_finishes_reads_then_embeds_only_those_documents() -> None:
    env = Env()
    parent = await env.parent([info("a.txt")])
    jobs = FakeJobQueue(FakeClock(NOW))
    handler = AttachmentJobs(reader(env, Fetcher({"k1-0": b"alpha beta"}, [])), env.ingest, jobs)
    assert await handler.enqueue(WS, [parent], hour=1)
    assert await handler.enqueue(WS, [parent], hour=1)  # same key: one job
    [job] = [j for j in jobs.jobs.values() if j.kind == READ_ATTACHMENTS]
    await handler.handle(job)
    child = (await env.index.attachment_children(WS, Principals((ADA,)), [parent]))[0][0]
    gen = await env.ingest._generations.ensure_present("fake-embed", 16)
    assert await env.index.chunks_without_embedding(WS, [child], gen, 10) == []
    assert any(e.kind == "embed" for e in env.ledger.entries)


async def test_lazy_embedding_respects_the_budget() -> None:  # edge case 31
    from core.domain.errors import BudgetExceeded

    env = Env()
    parent = await env.parent([info("a.txt")])
    env.ledger.spent = Decimal(5)  # the month's budget is used up after indexing
    await reader(env, Fetcher({"k1-0": b"alpha beta"}, [])).read(WS, None, parent, deadline=None)
    with pytest.raises(BudgetExceeded):
        await env.ingest.embed_documents(WS, [parent], data_class="private")


def test_email_keys_are_tied_to_their_message() -> None:  # IMAP UID reuse guard
    assert key_seed("<a@x>") != key_seed("<b@x>")
    assert len(key_seed("<a@x>")) == 12


def test_prepared_defaults_are_safe() -> None:
    p = Prepared(info("a.txt"))
    assert p.sections == ()
    assert p.checksum == ""


def _filters() -> SearchFilters:
    return SearchFilters()


async def _id(env: Env, ext: str) -> DocumentId:
    return env.index._by_key[(SRC, ext)]


# ---------------------------------------------------------------- review fixes


async def test_a_file_removed_from_a_discord_message_is_pruned() -> None:
    env = Env()
    await env.parent()
    a = AttachmentBlob(info("a.txt", key="k1-0"), b"alpha")
    b = AttachmentBlob(info("b.txt", key="k1-1"), b"bravo secret")
    await env.engine.write(env.ref(), await env.engine.prepare([a.info, b.info], [a, b]))
    assert await env.engine.prune(SRC, PARENT, [a.info]) == 1
    hits = await env.index.keyword_search(WS, Principals((ADA,)), "bravo", 5, _filters())
    assert hits == []
    assert await env.engine.prune(SRC, PARENT, [a.info]) == 0  # idempotent


async def test_a_resync_that_reset_the_list_does_not_download_again() -> None:
    env = Env()
    parent = await env.parent([info("a.txt")])
    f = Fetcher({"k1-0": b"alpha"}, [])
    r = reader(env, f)
    await r.read(WS, Principals((ADA,)), parent, deadline=None)
    # A sync rewrote the parent: its list says "listed" again, the child is still there.
    await env.index.set_attachments(SRC, PARENT, [info("a.txt")])
    out = await r.read(WS, Principals((ADA,)), parent, deadline=None)
    assert f.calls == ["k1-0"]
    assert out.attachments[0].state is AttachmentState.TEXT


async def test_an_empty_budget_ends_the_job_instead_of_retrying() -> None:
    env = Env()
    parent = await env.parent([info("a.txt")])
    env.ledger.spent = Decimal(5)
    jobs = FakeJobQueue(FakeClock(NOW))
    handler = AttachmentJobs(reader(env, Fetcher({"k1-0": b"alpha"}, [])), env.ingest, jobs)
    await handler.enqueue(WS, [parent], hour=1)
    [job] = list(jobs.jobs.values())
    await handler.handle(job)  # no BudgetExceeded: the text is read, vectors wait
    kids = await env.index.attachment_children(WS, Principals((ADA,)), [parent])
    assert len(kids) == 1
