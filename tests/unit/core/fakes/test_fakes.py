"""Fakes must satisfy their ports (checked by pyright) and behave sensibly (checked here)."""

from uuid import uuid4

import pytest

from core.domain.chat import ChatDelta, ChatMessage, Role, Usage
from core.domain.documents import DocumentUpsert, NewChunk, SearchFilters
from core.domain.errors import NotFound
from core.domain.ids import GenerationId, SourceId, UserId, WorkspaceId
from core.domain.principals import PUBLIC, Principals, user_principal
from core.ports.jobs import JobQueue, WorkerTrigger
from core.ports.models import ChatModel, Embedder
from core.ports.storage import BlobStore, DocumentStore, SearchIndex
from core.ports.system import Clock, HealthProbe, IdGenerator
from tests.fakes.jobs import FakeJobQueue
from tests.fakes.models import FakeChatModel, FakeEmbedder
from tests.fakes.storage import InMemoryBlobStore, InMemoryIndex
from tests.fakes.system import FakeClock, FakeHealthProbe, FakeWorkerTrigger, SequentialIds

# Static conformance: pyright fails the build if a fake drifts from its port.
_index = InMemoryIndex()
_doc_store: DocumentStore = _index
_search: SearchIndex = _index
_blob: BlobStore = InMemoryBlobStore()
_embedder: Embedder = FakeEmbedder()
_chat: ChatModel = FakeChatModel()
_queue: JobQueue = FakeJobQueue()
_trigger: WorkerTrigger = FakeWorkerTrigger()
_clock: Clock = FakeClock()
_ids: IdGenerator = SequentialIds()
_health: HealthProbe = FakeHealthProbe()

WS = WorkspaceId(uuid4())
OTHER_WS = WorkspaceId(uuid4())
SRC = SourceId(uuid4())
GEN = GenerationId(1)
OWNER = UserId(uuid4())
GUEST = UserId(uuid4())


def _doc(external_id: str, acl: tuple[str, ...], checksum: str = "c1") -> DocumentUpsert:
    return DocumentUpsert(
        source_id=SRC,
        external_id=external_id,
        uri=f"u/{external_id}",
        title=external_id,
        mime="text/plain",
        checksum=checksum,
        acl=acl,
    )


async def _seed(index: InMemoryIndex, embedder: FakeEmbedder) -> None:
    for ext, text, acl in [
        ("public-doc", "deploy the api with vercel", (PUBLIC,)),
        ("private-doc", "deploy secrets live in vault", (user_principal(OWNER),)),
    ]:
        doc_id = await index.upsert_document(WS, _doc(ext, acl))
        vecs = (await embedder.embed_documents([text])).vectors
        await index.replace_chunks(WS, doc_id, [NewChunk(0, text, 5)], {GEN: vecs})


async def test_embedder_is_deterministic_and_normalised() -> None:
    e = FakeEmbedder(dims=16)
    a, b = (await e.embed_documents(["hello", "hello"])).vectors
    assert a == b
    assert len(a) == 16
    assert abs(sum(x * x for x in a) - 1.0) < 1e-9


async def test_chat_model_streams_deltas_then_usage() -> None:
    model = FakeChatModel(reply="one two three")
    items = [i async for i in model.stream([ChatMessage(Role.USER, "q")], max_tokens=100)]
    assert "".join(i.text for i in items if isinstance(i, ChatDelta)) == "one two three"
    assert isinstance(items[-1], Usage)


async def test_index_upsert_is_idempotent_and_versions_on_checksum_change() -> None:
    idx = InMemoryIndex()
    a = await idx.upsert_document(WS, _doc("x", (PUBLIC,)))
    b = await idx.upsert_document(WS, _doc("x", (PUBLIC,)))
    assert a == b
    p = Principals((PUBLIC,))
    assert (await idx.get_document(WS, a, p)).version == 1
    await idx.upsert_document(WS, _doc("x", (PUBLIC,), checksum="c2"))
    assert (await idx.get_document(WS, a, p)).version == 2


async def test_search_never_returns_chunks_outside_principals() -> None:
    idx, emb = InMemoryIndex(), FakeEmbedder(dims=32)
    await _seed(idx, emb)
    guest = Principals.for_outsider(GUEST)
    owner = Principals.for_user(OWNER, WS)
    q = await emb.embed_query("deploy")
    guest_hits = await idx.vector_search(WS, guest, q, GEN, 10, SearchFilters())
    owner_hits = await idx.vector_search(WS, owner, q, GEN, 10, SearchFilters())
    assert {h.content for h in guest_hits} == {"deploy the api with vercel"}
    assert len(owner_hits) == 2
    kw = await idx.keyword_search(WS, guest, "deploy", 10, SearchFilters())
    assert all("vault" not in h.content for h in kw)


async def test_search_is_scoped_to_workspace() -> None:
    idx, emb = InMemoryIndex(), FakeEmbedder(dims=32)
    await _seed(idx, emb)
    hits = await idx.keyword_search(OTHER_WS, Principals((PUBLIC,)), "deploy", 10, SearchFilters())
    assert hits == []


async def test_get_document_hides_existence_of_forbidden_docs() -> None:
    idx, emb = InMemoryIndex(), FakeEmbedder(dims=32)
    await _seed(idx, emb)
    guest = Principals.for_outsider(GUEST)
    private_id = idx._by_key[(SRC, "private-doc")]
    with pytest.raises(NotFound):
        await idx.get_document(WS, private_id, guest)


async def test_visibility_change_rewrites_chunk_acls_without_reembedding() -> None:
    idx, emb = InMemoryIndex(), FakeEmbedder(dims=32)
    await _seed(idx, emb)
    calls_before = len(emb.calls)
    await idx.upsert_document(WS, _doc("private-doc", (PUBLIC,)))
    guest = Principals.for_outsider(GUEST)
    hits = await idx.keyword_search(WS, guest, "vault", 10, SearchFilters())
    assert len(hits) == 1
    assert len(emb.calls) == calls_before


async def test_blob_store_roundtrip_and_idempotent_delete() -> None:
    store = InMemoryBlobStore()
    ref = await store.put("a/b.txt", b"hi", "text/plain")
    assert await store.get(ref) == b"hi"
    await store.delete(ref)
    await store.delete(ref)
    with pytest.raises(NotFound):
        await store.get(ref)
