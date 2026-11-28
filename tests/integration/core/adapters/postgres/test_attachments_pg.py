"""ADR-0043 SQL against real Postgres: attachment children, conversation filters (sent,
people), the recent lane, and the lazy-embedding query. Reuses the search fixture."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from core.adapters.postgres.documents import PostgresDocumentStore
from core.adapters.postgres.search import PostgresSearchIndex
from core.adapters.postgres.sources import PostgresSourceStore
from core.adapters.postgres.uow import UnitOfWork
from core.domain.attachments import AttachmentInfo, AttachmentState
from core.domain.documents import DocumentUpsert, NewChunk, SearchFilters
from core.domain.errors import NotFound
from core.domain.ids import DocumentId
from core.domain.principals import Principals, user_principal
from tests.integration.core.adapters.postgres.test_search_pg import World

WHEN = datetime(2026, 11, 27, 9, 40, tzinfo=UTC)
PDF = AttachmentInfo(key="s1-1", name="rate-card.pdf", mime="application/pdf", size=10,
                     state=AttachmentState.TEXT)  # fmt: skip


async def _doc(
    uow: UnitOfWork, w: World, ext: str, body: str, *, meta: dict[str, object],
    parent: str | None = None, acl: tuple[str, ...] | None = None,
) -> DocumentId:  # fmt: skip
    doc_id, _ = await PostgresDocumentStore(uow).write_document(
        w.ws,
        DocumentUpsert(
            w.source, ext, f"mail:{ext}", ext, "text/plain", ext,
            acl or (user_principal(w.owner),), author="Daniel Brooks",
            source_updated_at=WHEN, metadata=meta, parent_external_id=parent,
        ),
        [NewChunk(0, body, len(body.split()))],
        {},
        data_class="private",
    )  # fmt: skip
    return doc_id


async def _mail(uow: UnitOfWork, w: World) -> tuple[DocumentId, DocumentId]:
    parent = await _doc(
        uow, w, "thread:rate", "From Daniel to Lena Park: (no text) [attachment: rate-card.pdf]",
        meta={"folders": ["sent"], "people": ["Daniel Brooks", "Lena Park <lena@x.example>"],
              "attachments": [PDF.to_meta()]},
    )  # fmt: skip
    child = await _doc(
        uow, w, "thread:rate#att/s1-1", "Less than truckload pallets cost 95 EUR each.",
        meta={"kind": "attachment"}, parent="thread:rate", acl=("public",),  # it asks for more
    )  # fmt: skip
    await _doc(uow, w, "thread:in", "From Priya: pallets invoice", meta={"folders": ["inbox"],
               "people": ["Priya Shah"]})  # fmt: skip
    return parent, child


def owner(w: World) -> Principals:
    return Principals.for_user(w.owner, w.ws)


async def test_an_attachment_is_found_with_its_parent_and_its_acl(
    uow: UnitOfWork, world: World
) -> None:
    parent, child = await _mail(uow, world)
    index = PostgresSearchIndex(uow)
    [hit] = await index.keyword_search(world.ws, owner(world), "truckload", 5, SearchFilters())
    assert hit.document_id == child
    assert hit.parent_id == parent
    # The child asked for a public ACL; the database gave it the parent's.
    assert (
        await index.keyword_search(world.ws, world.guest_p(), "truckload", 5, SearchFilters()) == []
    )
    kids = await index.attachment_children(world.ws, owner(world), [parent])
    assert kids == [(child, parent)]
    assert await index.attachment_children(world.ws, world.guest_p(), [parent]) == []


async def test_sent_and_person_filters_read_the_conversation(uow: UnitOfWork, world: World) -> None:
    parent, child = await _mail(uow, world)
    index = PostgresSearchIndex(uow)
    sent = await index.keyword_search(
        world.ws, owner(world), "pallets", 10, SearchFilters(sent=True)
    )
    assert {h.document_id for h in sent} == {child}  # the attachment matches via its parent
    received = await index.keyword_search(
        world.ws, owner(world), "pallets", 10, SearchFilters(sent=False)
    )
    assert all(h.document_id not in (parent, child) for h in received)
    lena = await index.keyword_search(
        world.ws, owner(world), "pallets", 10, SearchFilters(person="lena")
    )
    assert {h.document_id for h in lena} == {child}
    # LIKE wildcards in the user's words are literal.
    assert (
        await index.keyword_search(world.ws, owner(world), "pallets", 10, SearchFilters(person="%"))
        == []
    )


async def test_the_recent_lane_lists_conversations_not_their_attachments(
    uow: UnitOfWork, world: World
) -> None:
    parent, child = await _mail(uow, world)
    hits = await PostgresSearchIndex(uow).recent_chunks(world.ws, owner(world), 10, SearchFilters())
    ids = {h.document_id for h in hits}
    assert parent in ids
    assert child not in ids
    [p] = [h for h in hits if h.document_id == parent][:1]
    assert [a.name for a in p.attachments] == ["rate-card.pdf"]


async def test_children_are_invisible_to_sync_listings_and_follow_deletion(
    uow: UnitOfWork, world: World
) -> None:
    _, child = await _mail(uow, world)
    sources = PostgresSourceStore(uow)
    listed = await sources.document_checksums(world.source, "thread:")
    assert "thread:rate#att/s1-1" not in listed
    docs = PostgresDocumentStore(uow)
    assert list(await docs.child_checksums(world.source, "thread:rate")) == ["thread:rate#att/s1-1"]
    await sources.delete_documents(world.source, ["thread:rate"])
    async with uow() as s:
        left = (await s.execute(text("SELECT count(*) FROM documents WHERE id = :c"),
                                {"c": child})).scalar_one()  # fmt: skip
    assert left == 0


async def test_set_attachments_and_the_parent_lookup(uow: UnitOfWork, world: World) -> None:
    parent, _ = await _mail(uow, world)
    docs = PostgresDocumentStore(uow)
    gone = PDF.with_state(AttachmentState.GONE)
    await docs.set_attachments(world.source, "thread:rate", [gone])
    found = await docs.attachment_parent(world.ws, owner(world), parent)
    assert [a.state for a in found.attachments] == [AttachmentState.GONE]
    assert found.acl == (user_principal(world.owner),)
    with pytest.raises(NotFound):  # someone who can't see it gets the same as missing
        await docs.attachment_parent(world.ws, world.guest_p(), parent)


async def test_lazy_embedding_finds_a_parents_attachment_chunks(
    uow: UnitOfWork, world: World
) -> None:
    parent, _ = await _mail(uow, world)
    docs = PostgresDocumentStore(uow)
    todo = await docs.chunks_without_embedding(world.ws, [parent], world.gen, 50)
    assert {t.content for t in todo} >= {"Less than truckload pallets cost 95 EUR each."}
    other = await docs.chunks_without_embedding(world.other_ws, [parent], world.gen, 50)
    assert other == []  # never another workspace's chunks
