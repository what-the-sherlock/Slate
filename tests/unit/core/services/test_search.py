from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from uuid import uuid4

import pytest

from core.domain.auth import Identity, Role
from core.domain.budget import SpendLimits
from core.domain.documents import DocumentUpsert, NewChunk, SearchFilters, Vector
from core.domain.errors import NotFound, RateLimited, UpstreamUnavailable, ValidationFailed
from core.domain.ids import GenerationId, SessionId, SourceId, UserId, WorkspaceId
from core.domain.principals import PUBLIC, Principals, user_principal, workspace_principal
from core.domain.search import Lane
from core.services.search import SEARCH_LIMIT, SearchService
from tests.fakes.models import FakeEmbedder
from tests.fakes.rate_limit import InMemoryRateLimiter
from tests.fakes.storage import InMemoryIndex
from tests.fakes.system import FakeClock
from tests.fakes.usage import InMemoryGenerations, InMemoryLedger

WS = WorkspaceId(uuid4())
OWNER = UserId(uuid4())
GUEST = UserId(uuid4())
SOURCE = SourceId(uuid4())
GEN = GenerationId(1)


def identity(user: UserId, *, guest: bool = False) -> Identity:
    return Identity(
        user_id=user,
        workspace_id=WS,
        session_id=SessionId(uuid4()),
        email="a@b.c",
        name="A",
        role=Role.VIEWER if guest else Role.OWNER,
        email_verified=True,
        principals=(Principals.for_outsider(user) if guest else Principals.for_user(user, WS)),
        csrf_secret="c",
    )


class DownEmbedder(FakeEmbedder):
    async def embed_query(self, text: str) -> Vector:
        raise UpstreamUnavailable("embedder down")


async def seed(index: InMemoryIndex, embedder: FakeEmbedder) -> None:
    docs: Sequence[tuple[str, str, tuple[str, ...]]] = [
        ("handbook", "Everyone gets 25 days of paid leave per year.", (PUBLIC,)),
        ("salaries", "Salary bands for leave cover are confidential.", (user_principal(OWNER),)),
        ("team", "The team meets on Mondays.", (workspace_principal(WS),)),
    ]
    for ext, content, acl in docs:
        vec = (await embedder.embed_documents([content])).vectors
        await index.write_document(
            WS,
            DocumentUpsert(SOURCE, ext, f"upload://{ext}", ext.title(), "text/plain", ext, acl),
            [NewChunk(0, content, len(content.split()))],
            {GEN: vec},
            data_class="private",
        )


def make(
    embedder: FakeEmbedder | None = None, *, spent: str = "0", present: bool = True
) -> tuple[SearchService, InMemoryIndex, FakeEmbedder]:
    e = embedder or FakeEmbedder()
    index = InMemoryIndex()
    gens = InMemoryGenerations()
    if present:
        gens.present_gen = (GEN, e.model)
    svc = SearchService(
        index,
        e,
        gens,
        InMemoryLedger(Decimal(spent)),
        InMemoryRateLimiter(FakeClock()),
        limits=SpendLimits.monthly_only(Decimal(5)),
    )
    return svc, index, e


async def test_hybrid_search_runs_both_lanes_and_fuses() -> None:
    svc, index, e = make()
    await seed(index, e)
    # The exact chunk text as the query: its fake vector matches, and the words match.
    res = await svc.search(identity(OWNER), "Everyone gets 25 days of paid leave per year.")
    assert set(res.lanes) == {Lane.KEYWORD, Lane.VECTOR}
    top = res.hits[0]
    assert top.chunk.title == "Handbook"
    assert top.ranks == {Lane.KEYWORD: 1, Lane.VECTOR: 1}
    assert res.notices == ()


async def test_guest_never_sees_private_or_workspace_chunks() -> None:
    svc, index, e = make()
    await seed(index, e)
    res = await svc.search(identity(GUEST, guest=True), "leave team salary Mondays")
    titles = {h.chunk.title for h in res.hits}
    assert titles <= {"Handbook"}
    owner = await svc.search(identity(OWNER), "leave team salary Mondays")
    assert {h.chunk.title for h in owner.hits} >= {"Salaries", "Team"}


async def test_keyword_only_before_any_generation_exists() -> None:
    svc, index, e = make(present=False)
    await seed(index, e)
    res = await svc.search(identity(OWNER), "leave")
    assert res.lanes == (Lane.KEYWORD,)
    assert res.notices == ()  # not a degradation, just nothing embedded yet


async def test_generation_from_another_model_is_not_queried() -> None:
    svc, index, e = make(FakeEmbedder(model="other-model"))
    svc._generations.present_gen = (GEN, "fake-embed")  # type: ignore[attr-defined]
    await seed(index, e)
    res = await svc.search(identity(OWNER), "leave")
    assert res.lanes == (Lane.KEYWORD,)


async def test_degrades_to_keyword_when_embedder_is_down() -> None:
    svc, index, _ = make(DownEmbedder())
    await seed(index, FakeEmbedder())
    res = await svc.search(identity(OWNER), "leave")
    assert res.lanes == (Lane.KEYWORD,)
    assert "unavailable" in res.notices[0]
    assert res.hits


async def test_degrades_to_keyword_when_budget_is_spent() -> None:
    svc, index, e = make(spent="5")
    await seed(index, e)
    res = await svc.search(identity(OWNER), "leave")
    assert res.lanes == (Lane.KEYWORD,)
    assert "budget" in res.notices[0]


@pytest.mark.parametrize("query", ["", "   \n\t ", "x" * 501])
async def test_rejects_empty_and_oversized_queries(query: str) -> None:
    svc, _, _ = make()
    with pytest.raises(ValidationFailed):
        await svc.search(identity(OWNER), query)


async def test_limit_is_clamped_and_filters_pass_through() -> None:
    svc, index, e = make()
    await seed(index, e)
    res = await svc.search(identity(OWNER), "leave", limit=0)
    assert len(res.hits) == 1
    none = await svc.search(
        identity(OWNER), "leave", filters=SearchFilters(source_ids=(SourceId(uuid4()),))
    )
    assert none.hits == []


async def test_keyword_only_without_an_embedder() -> None:
    svc, index, e = make()
    await seed(index, e)
    svc._embedder = None  # as wired when no OpenAI key is set
    res = await svc.search(identity(OWNER), "leave")
    assert res.lanes == (Lane.KEYWORD,)
    assert res.hits


async def test_searches_are_rate_limited_per_user() -> None:
    svc, _, _ = make()
    for _ in range(SEARCH_LIMIT.budget):
        await svc.search(identity(OWNER), "leave")
    with pytest.raises(RateLimited):
        await svc.search(identity(OWNER), "leave")
    await svc.search(identity(GUEST, guest=True), "leave")  # other users unaffected


async def test_too_many_source_filters_are_refused() -> None:
    svc, _, _ = make()
    many = SearchFilters(source_ids=tuple(SourceId(uuid4()) for _ in range(21)))
    with pytest.raises(ValidationFailed, match="at most 20"):
        await svc.search(identity(OWNER), "leave", filters=many)


async def test_preview_rechecks_access() -> None:
    svc, index, e = make()
    await seed(index, e)
    res = await svc.search(identity(OWNER), "Salary bands")
    private = next(h for h in res.hits if h.chunk.title == "Salaries")
    preview = await svc.preview(identity(OWNER), private.document_id)
    assert preview.chunks[0].content.startswith("Salary bands")
    with pytest.raises(NotFound):
        await svc.preview(identity(GUEST, guest=True), private.document_id)


async def test_degrades_to_keyword_when_this_workspace_used_its_allowance() -> None:
    e = FakeEmbedder()
    index, gens, ledger = InMemoryIndex(), InMemoryGenerations(), InMemoryLedger()
    gens.present_gen = (GEN, e.model)
    ledger.workspace_spent[WS] = Decimal("0.05")  # today's workspace allowance, all used
    svc = SearchService(
        index, e, gens, ledger, InMemoryRateLimiter(FakeClock()),
        limits=SpendLimits(monthly_all=Decimal(5), daily_workspace=Decimal("0.05")),
    )  # fmt: skip
    await seed(index, e)
    res = await svc.search(identity(OWNER), "leave")
    assert res.lanes == (Lane.KEYWORD,)
    assert "today's AI allowance" in res.notices[0]
    assert res.hits  # keyword search still answers


async def test_vector_searches_are_recorded_so_the_caps_can_stop_them() -> None:
    svc, index, e = make()
    await seed(index, e)
    ledger = svc._ledger
    await svc.search(identity(OWNER), "paid leave")
    entries = ledger.entries  # type: ignore[attr-defined]
    assert len(entries) == 1
    assert entries[0].workspace_id == WS
    assert entries[0].data_class == "private"  # a query may be about private content
    assert entries[0].cost_usd > 0


async def test_keyword_only_searches_cost_nothing() -> None:
    svc, index, e = make(present=False)
    await seed(index, e)
    await svc.search(identity(OWNER), "leave")
    assert svc._ledger.entries == []  # type: ignore[attr-defined]


async def test_author_filter_is_length_checked() -> None:
    from core.domain.documents import SearchFilters
    from core.domain.errors import ValidationFailed

    svc, _, _ = make()
    with pytest.raises(ValidationFailed):
        await svc.search(identity(OWNER), "leave", filters=SearchFilters(author="x" * 201))


async def test_the_anchor_lane_ranks_cited_documents_and_keeps_the_acl() -> None:
    """P1-5: hybrid_retrieve fuses the previous answer's documents as their own lane, and
    the lane itself filters by access (an outsider's anchor finds nothing private)."""
    from core.services.search import hybrid_retrieve

    index, embedder = InMemoryIndex(), FakeEmbedder()
    await seed(index, embedder)
    docs = {ext: index._by_key[(SOURCE, ext)] for ext in ("handbook", "salaries", "team")}
    anchor = tuple(docs.values())
    owner = identity(OWNER).principals
    res = await hybrid_retrieve(
        index, WS, owner, "zzz", SearchFilters(), vector=None, anchor=anchor
    )
    assert Lane.ANCHOR in res.lanes
    assert {h.chunk.document_id for h in res.hits} == set(anchor)  # through the anchor alone
    guest = identity(GUEST, guest=True).principals
    as_guest = await hybrid_retrieve(
        index, WS, guest, "zzz", SearchFilters(), vector=None, anchor=anchor
    )
    assert {h.chunk.document_id for h in as_guest.hits} == {docs["handbook"]}  # public only
